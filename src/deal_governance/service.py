"""授权版本、签署前检查、收益预测、逐期确认与争议冻结的应用服务。

关键不变量：
- term_revisions 一旦 effective 即不可变；修订只能新增版本，旧版本标记 superseded。
- 每次修订携带 source_revision_id，指向签署当时上游有效的权利来源版本。
- payment_allocations 中 settled 行永久不可变；争议只冻结涉及的 pending 行，
  裁决结果以 voided + adjustment 行留痕，绝不修改历史金额。
- 到期（expiry_date < as_of）的版本不再视为有效，续约必须显式签署新版本，
  系统不会因为续约谈判而延长旧权利。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, date_text, utc_text
from .economics import build_projection_lines, projection_total, waterfall_allocations
from .errors import Conflict, Forbidden, InvalidState, NotFound, SigningBlocked, ValidationFailed
from .models import Candidate, Party, Scope, TermRevision
from .storage import SCHEMA, canonical_json, connect, digest, rows, transaction, utcnow_text

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "bd": {
        "catalog.write", "agreement.write", "agreement.sign",
        "obligation.write", "report.read",
    },
    "manager": {
        "catalog.write", "agreement.write", "agreement.sign",
        "obligation.write", "report.read", "audit.read",
    },
    "finance": {
        "projection.write", "projection.commit", "payment.write",
        "payment.settle", "dispute.write", "dispute.resolve", "report.read",
    },
    "auditor": {"report.read", "audit.read"},
    "admin": {
        "catalog.write", "agreement.write", "agreement.sign",
        "obligation.write", "projection.write", "projection.commit",
        "payment.write", "payment.settle", "dispute.write",
        "dispute.resolve", "report.read", "audit.read",
    },
}

PAYMENT_KIND_LABELS = {"upfront", "milestone", "royalty", "sublicense"}


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


class DealGovernanceService:
    def __init__(self, connection=None, clock=None, *, database: str | None = None) -> None:
        self.clock = clock or SystemClock()
        self._thread_local = threading.local()
        if connection is not None:
            # 进程内（测试/CLI）共享同一连接
            self._shared = connection
            self._database_path = None
            self._shared.executescript(SCHEMA)
            self._shared.commit()
        else:
            self._shared = None
            self._database_path = database
            # 预建表，保证首个请求前结构就绪
            boot = self._new_connection()
            boot.close()

    def _new_connection(self) -> sqlite3.Connection:
        db = connect(self._database_path or ":memory:")
        db.executescript(SCHEMA)
        db.commit()
        return db

    @property
    def db(self) -> sqlite3.Connection:
        if self._shared is not None:
            return self._shared
        connection = getattr(self._thread_local, "connection", None)
        if connection is None:
            connection = self._new_connection()
            self._thread_local.connection = connection
        return connection

    # ------------------------------------------------------------------ 用户

    def _user(self, actor_id: str):
        row = self.db.execute("SELECT * FROM dg_users WHERE user_id=?", (actor_id,)).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, actor_id: str, permission: str):
        user = self._user(actor_id)
        if permission not in ROLE_PERMISSIONS.get(user["role"], set()):
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def create_user(self, actor_id: str | None, user_id: str, display_name: str, role: str) -> dict:
        if actor_id is not None:
            self._require(actor_id, "catalog.write")
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.db):
                self.db.execute(
                    "INSERT INTO dg_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, utcnow_text()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        self._audit("user", user_id, "user.created", actor_id or user_id, {"role": role})
        return {"user_id": user_id.strip(), "role": role}

    # ------------------------------------------------------------------ 审计

    def _audit(self, entity_type: str, entity_id: str, event_type: str,
               actor_id: str, payload: Mapping[str, Any]) -> None:
        previous = self.db.execute(
            "SELECT event_hash FROM dg_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        created_at = utc_text(self.clock.now()) if hasattr(self.clock, "now") else utcnow_text()
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": created_at,
            "previous_hash": previous_hash,
        }
        self.db.execute(
            "INSERT INTO dg_audit_events(entity_type,entity_id,event_type,actor_id,"
            "payload_json,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (entity_type, entity_id, event_type, actor_id,
             canonical_json(payload), previous_hash, digest(body), created_at),
        )

    def audit_chain(self, actor_id: str) -> list[dict]:
        self._require(actor_id, "audit.read")
        return rows(self.db, "SELECT * FROM dg_audit_events ORDER BY event_id")

    # ------------------------------------------------------------ 候选药/方

    def register_candidate(self, actor_id: str, raw: Mapping[str, Any]) -> dict:
        self._require(actor_id, "catalog.write")
        candidate = Candidate.from_dict(raw)
        try:
            with transaction(self.db):
                self.db.execute(
                    "INSERT INTO candidates(candidate_id,name,created_at,created_by) VALUES(?,?,?,?)",
                    (candidate.candidate_id, candidate.name, utcnow_text(), actor_id),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("候选药已经登记") from exc
        self._audit("candidate", candidate.candidate_id, "candidate.registered", actor_id,
                    {"name": candidate.name})
        return {"candidate_id": candidate.candidate_id, "name": candidate.name}

    def register_party(self, actor_id: str, raw: Mapping[str, Any]) -> dict:
        self._require(actor_id, "catalog.write")
        party = Party.from_dict(raw)
        try:
            with transaction(self.db):
                self.db.execute(
                    "INSERT INTO parties(party_id,name,created_at,created_by) VALUES(?,?,?,?)",
                    (party.party_id, party.name, utcnow_text(), actor_id),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("交易方已经登记") from exc
        self._audit("party", party.party_id, "party.registered", actor_id, {"name": party.name})
        return {"party_id": party.party_id, "name": party.name}

    # ------------------------------------------------------------- 合同与版本

    def create_agreement(self, actor_id: str, agreement_id: str, candidate_id: str, title: str) -> dict:
        self._require(actor_id, "agreement.write")
        if not self.db.execute("SELECT 1 FROM candidates WHERE candidate_id=?", (candidate_id,)).fetchone():
            raise NotFound("候选药不存在")
        try:
            with transaction(self.db):
                self.db.execute(
                    "INSERT INTO agreements(agreement_id,candidate_id,title,state,created_at,created_by)"
                    " VALUES(?,?,?,'negotiating',?,?)",
                    (agreement_id, candidate_id, title, utcnow_text(), actor_id),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("合同已经存在") from exc
        self._audit("agreement", agreement_id, "agreement.created", actor_id,
                    {"candidate_id": candidate_id, "title": title})
        return {"agreement_id": agreement_id, "candidate_id": candidate_id, "state": "negotiating"}

    def _get_agreement(self, agreement_id: str):
        row = self.db.execute("SELECT * FROM agreements WHERE agreement_id=?", (agreement_id,)).fetchone()
        if row is None:
            raise NotFound("合同不存在")
        return row

    def draft_revision(self, actor_id: str, agreement_id: str, raw: Mapping[str, Any]) -> dict:
        """登记一条条款修订（草稿）。revision_no 由系统按合同递增。"""
        self._require(actor_id, "agreement.write")
        agreement = self._get_agreement(agreement_id)
        if agreement["state"] == "terminated":
            raise InvalidState("合同已终止，不能再修订")

        last = self.db.execute(
            "SELECT MAX(revision_no) AS rev FROM term_revisions WHERE agreement_id=?",
            (agreement_id,),
        ).fetchone()
        next_no = (last["rev"] or 0) + 1
        term = TermRevision.from_dict(raw, next_no)

        for party_id in (term.licensor_party_id, term.licensee_party_id):
            if not self.db.execute("SELECT 1 FROM parties WHERE party_id=?", (party_id,)).fetchone():
                raise NotFound(f"交易方 {party_id} 未登记")
        if term.licensor_party_id == term.licensee_party_id:
            raise ValidationFailed("授权方与被授权方不能相同")

        self._validate_source(agreement["candidate_id"], term)

        snapshot = self._term_snapshot(term)
        snapshot_hash = digest(snapshot)
        revision_id = f"{agreement_id}-r{next_no}"
        created_at = utcnow_text()

        scope_json = canonical_json(term.scope.as_dict())
        royalty_json = canonical_json(self._royalty_json(term)) if term.royalty else None

        with transaction(self.db):
            self.db.execute(
                "INSERT INTO term_revisions(revision_id,agreement_id,revision_no,state,"
                "effective_date,expiry_date,licensor_party_id,licensee_party_id,scope_json,"
                "exclusivity,sublicense_scope,upfront_amount,royalty_json,sublicense_income_share,"
                "source_revision_id,snapshot_json,snapshot_hash,change_note,created_by,created_at)"
                " VALUES(?,?,?,'draft',?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (revision_id, agreement_id, next_no, term.effective_date, term.expiry_date,
                 term.licensor_party_id, term.licensee_party_id, scope_json,
                 term.exclusivity, term.sublicense_scope, str(term.upfront_amount),
                 royalty_json, str(term.sublicense_income_share),
                 term.source_revision_id, canonical_json(snapshot), snapshot_hash,
                 term.change_note, actor_id, created_at),
            )
            for obligation in term.obligations:
                self.db.execute(
                    "INSERT INTO obligations(obligation_uid,revision_id,agreement_id,obligation_id,"
                    "kind,description,due_date,due_event,owner_party_id,state)"
                    " VALUES(?,?,?,?,?,?,?,?,?, 'open')",
                    (_new_id("obl"), revision_id, agreement_id, obligation.obligation_id,
                     obligation.kind, obligation.description, obligation.due_date,
                     obligation.due_event, obligation.owner_party_id),
                )
        self._audit("term_revision", revision_id, "revision.drafted", actor_id, {
            "agreement_id": agreement_id, "revision_no": next_no,
            "source_revision_id": term.source_revision_id, "snapshot_hash": snapshot_hash,
        })
        return self.get_revision(actor_id, revision_id)

    def _validate_source(self, candidate_id: str, term: TermRevision) -> None:
        """每次修订必须引用签署当时有效的权利来源版本，且范围不得越权。"""
        if term.source_revision_id is None:
            return  # 初始（源头）授权
        source = self.db.execute(
            "SELECT tr.*, a.candidate_id FROM term_revisions tr JOIN agreements a"
            " ON a.agreement_id=tr.agreement_id WHERE tr.revision_id=?",
            (term.source_revision_id,),
        ).fetchone()
        if source is None:
            raise NotFound("source_revision_id 指向的版本不存在")
        if source["state"] != "effective":
            raise InvalidState("权利来源版本在签署当时不是有效版本")
        if source["candidate_id"] != candidate_id:
            raise ValidationFailed("权利来源必须属于同一候选药")
        if term.effective_date < source["effective_date"]:
            raise ValidationFailed("再许可生效日不能早于权利来源生效日")
        if source["expiry_date"] and (
            not term.expiry_date or term.expiry_date > source["expiry_date"]
        ):
            raise ValidationFailed("再许可期限不得超过权利来源期限，续约不能自动延长旧权利")
        if source["sublicense_scope"] == "none":
            raise ValidationFailed("上游版本不允许再许可")
        source_snapshot = json.loads(source["snapshot_json"])
        source_scope = Scope.from_dict(source_snapshot["scope"])
        if not source_scope.contains(term.scope):
            raise ValidationFailed("再许可范围超出上游授权范围")
        if source["sublicense_scope"] == "negotiate":
            # 仅允许排他性不高于上游的安排：exclusive 不能来自 negotiate
            if term.exclusivity == "exclusive" and source["exclusivity"] != "exclusive":
                raise ValidationFailed("negotiate 来源未授予排他权利，不能签发排他再许可")

    # ------------------------------------------------------- 签署前冲突检查

    def pre_sign_findings(self, actor_id: str, revision_id: str) -> list[dict]:
        self._require(actor_id, "agreement.sign")
        revision = self._get_revision_row(revision_id)
        if revision["state"] != "draft":
            raise InvalidState("只有草稿版本需要签署前检查")
        return self._compute_findings(revision)

    def _effective_siblings(self, revision, *, include_self: bool = False):
        """同一候选药在生效日时点有效的其他版本（as-of effective_date）。"""
        as_of = revision["effective_date"]
        rows_ = self.db.execute(
            "SELECT tr.* FROM term_revisions tr JOIN agreements a"
            " ON a.agreement_id=tr.agreement_id WHERE a.candidate_id=? AND tr.state IN ('effective','draft')",
            (revision["candidate_id"],),
        ).fetchall()
        result = []
        for other in rows_:
            if other["revision_id"] == revision["revision_id"] and not include_self:
                continue
            # as-of 生效判断：other.effective_date <= as_of，且未在 as_of 前到期
            if other["effective_date"] > as_of:
                continue
            if other["expiry_date"] and other["expiry_date"] < as_of:
                continue
            result.append(other)
        return result

    def _lineage_agreement_ids(self, revision_row) -> set[str]:
        """沿 source_revision_id 上溯涉及的全部合同 id（不做时点校验）。"""
        ids = {revision_row["agreement_id"]}
        current = revision_row
        seen: set[str] = set()
        while current["source_revision_id"] and current["source_revision_id"] not in seen:
            seen.add(current["source_revision_id"])
            source = self.db.execute(
                "SELECT * FROM term_revisions WHERE revision_id=?",
                (current["source_revision_id"],),
            ).fetchone()
            if source is None:
                break
            ids.add(source["agreement_id"])
            current = source
        return ids

    def _is_related_lineage(self, other, revision) -> bool:
        other_lineage = self._lineage_agreement_ids(other)
        if revision["agreement_id"] in other_lineage:
            return True
        return other["agreement_id"] in self._lineage_agreement_ids(revision)

    def _compute_findings(self, revision) -> list[dict]:
        findings: list[dict] = []
        target_scope = Scope.from_dict(json.loads(revision["scope_json"]))
        for other in self._effective_siblings(revision):
            if other["agreement_id"] == revision["agreement_id"]:
                # 同一合同的修订以新版本取代旧版本，不构成跨合同冲突
                continue
            if self._is_related_lineage(other, revision):
                # 对方是上游授权方或本合同的下游再许可，重叠属授权链内部
                continue
            other_scope = Scope.from_dict(json.loads(other["scope_json"]))
            if not target_scope.overlaps(other_scope):
                continue
            detail = target_scope.overlap_detail(other_scope)
            same_party_pair = (
                revision["licensee_party_id"] == other["licensee_party_id"]
                and revision["licensor_party_id"] == other["licensor_party_id"]
            )
            # 排他冲突：任一版本 exclusive/sole，重叠范围内再授权即冲突
            exclusive_types = {"exclusive", "sole"}
            if revision["exclusivity"] in exclusive_types or other["exclusivity"] in exclusive_types:
                severity = "blocker"
                code = "exclusivity_conflict"
                message = (f"与版本 {other['revision_id']} 在同一地区/适应症/阶段重叠且存在排他权利："
                           f"{detail}")
                if same_party_pair:
                    # 同一对交易方的重叠多为范围重复登记
                    code = "scope_overlap"
                    severity = "blocker"
                    message = f"与版本 {other['revision_id']} 范围完全重叠（重复授权）：{detail}"
                findings.append({
                    "code": code, "severity": severity, "revision_id": other["revision_id"],
                    "overlap": detail, "message": message,
                })
            else:
                findings.append({
                    "code": "scope_overlap", "severity": "warning",
                    "revision_id": other["revision_id"], "overlap": detail,
                    "message": f"与非排他版本 {other['revision_id']} 范围重叠，需要确认不冲突：{detail}",
                })

        # 义务缺口：授权范围覆盖临床阶段但无开发/监管义务
        snapshot = json.loads(revision["snapshot_json"])
        obligation_kinds = {o["kind"] for o in snapshot["obligations"]}
        clinical_stages = {"phase1", "phase2", "phase3", "filing"}
        if set(target_scope.stages) & clinical_stages:
            missing = []
            if not (obligation_kinds & {"development", "regulatory"}):
                missing.append("development/regulatory")
            if revision["upfront_amount"] != "0.00" and "payment" not in obligation_kinds:
                # 首付款由付款事件确认，不强制义务；里程碑/分成无支付节点约束时提示
                pass
            if not obligation_kinds & {"commercial"} and "approved" in target_scope.stages:
                missing.append("commercial")
            for gap in missing:
                findings.append({
                    "code": "obligation_gap", "severity": "blocker",
                    "missing": gap,
                    "message": f"授权覆盖对应阶段但未约定 {gap} 义务，存在义务缺口",
                })

        # 经济条款缺口：分成阶段却无 royalty，或允许再许可却无收入分成比例
        if "approved" in target_scope.stages and not snapshot.get("royalty"):
            findings.append({
                "code": "obligation_gap", "severity": "warning", "missing": "royalty",
                "message": "范围覆盖获批上市阶段但未约定销售分成条款",
            })
        if revision["sublicense_scope"] != "none" and Decimal(revision["sublicense_income_share"]) == 0:
            findings.append({
                "code": "obligation_gap", "severity": "warning", "missing": "sublicense_share",
                "message": "允许再许可但再许可收入分成比例为 0，请确认",
            })
        return findings

    def sign_revision(self, actor_id: str, revision_id: str) -> dict:
        """签署生效：存在 blocker（排他冲突/范围重叠/义务缺口）时拒绝。"""
        self._require(actor_id, "agreement.sign")
        revision = self._get_revision_row(revision_id)
        if revision["state"] != "draft":
            raise InvalidState("版本不是草稿，不能签署")
        findings = self._compute_findings(revision)
        blockers = [f for f in findings if f["severity"] == "blocker"]
        if blockers:
            raise SigningBlocked(findings)

        signed_at = utc_text(self.clock.now())
        with transaction(self.db):
            # 同合同旧版本 superseded（版本化修订，不删除旧版本）
            self.db.execute(
                "UPDATE term_revisions SET state='superseded', superseded_at=? "
                "WHERE agreement_id=? AND state='effective'",
                (signed_at, revision["agreement_id"]),
            )
            self.db.execute(
                "UPDATE term_revisions SET state='effective', signed_at=? WHERE revision_id=?",
                (signed_at, revision_id),
            )
            self.db.execute(
                "UPDATE agreements SET state='active' WHERE agreement_id=?",
                (revision["agreement_id"],),
            )
        self._audit("term_revision", revision_id, "revision.signed", actor_id, {
            "agreement_id": revision["agreement_id"], "signed_at": signed_at,
            "warnings": [f for f in findings if f["severity"] == "warning"],
        })
        return self.get_revision(actor_id, revision_id)

    def terminate_revision(self, actor_id: str, revision_id: str, reason: str) -> dict:
        self._require(actor_id, "agreement.sign")
        revision = self._get_revision_row(revision_id)
        if revision["state"] not in {"effective", "draft"}:
            raise InvalidState("版本已被取代或终止")
        with transaction(self.db):
            self.db.execute(
                "UPDATE term_revisions SET state='terminated', terminated_at=? WHERE revision_id=?",
                (utc_text(self.clock.now()), revision_id),
            )
            agreement = self._get_agreement(revision["agreement_id"])
            if agreement["state"] == "active":
                still_effective = self.db.execute(
                    "SELECT 1 FROM term_revisions WHERE agreement_id=? AND state='effective'",
                    (revision["agreement_id"],),
                ).fetchone()
                if not still_effective:
                    self.db.execute(
                        "UPDATE agreements SET state='terminated' WHERE agreement_id=?",
                        (revision["agreement_id"],),
                    )
        self._audit("term_revision", revision_id, "revision.terminated", actor_id, {"reason": reason})
        return self.get_revision(actor_id, revision_id)

    def _get_revision_row(self, revision_id: str):
        row = self.db.execute(
            "SELECT tr.*, a.candidate_id FROM term_revisions tr JOIN agreements a"
            " ON a.agreement_id=tr.agreement_id WHERE tr.revision_id=?",
            (revision_id,),
        ).fetchone()
        if row is None:
            raise NotFound("条款版本不存在")
        return row

    @staticmethod
    def _term_snapshot(term: TermRevision) -> dict:
        return {
            "revision_no": term.revision_no,
            "effective_date": term.effective_date,
            "expiry_date": term.expiry_date,
            "licensor_party_id": term.licensor_party_id,
            "licensee_party_id": term.licensee_party_id,
            "scope": term.scope.as_dict(),
            "exclusivity": term.exclusivity,
            "sublicense_scope": term.sublicense_scope,
            "upfront_amount": str(term.upfront_amount),
            "milestones": [
                {
                    "milestone_id": m.milestone_id, "milestone_type": m.milestone_type,
                    "name": m.name, "amount": str(m.amount), "trigger_event": m.trigger_event,
                }
                for m in term.milestones
            ],
            "royalty": DealGovernanceService._royalty_json(term) if term.royalty else None,
            "sublicense_income_share": str(term.sublicense_income_share),
            "obligations": [
                {
                    "obligation_id": o.obligation_id, "kind": o.kind,
                    "description": o.description, "due_date": o.due_date,
                    "due_event": o.due_event, "owner_party_id": o.owner_party_id,
                }
                for o in term.obligations
            ],
            "source_revision_id": term.source_revision_id,
            "change_note": term.change_note,
        }

    @staticmethod
    def _royalty_json(term: TermRevision) -> dict | None:
        if term.royalty is None:
            return None
        return {"rate": str(term.royalty.rate),
                "cap_rate": str(term.royalty.cap_rate) if term.royalty.cap_rate else None}

    def get_revision(self, actor_id: str | None, revision_id: str) -> dict:
        if actor_id is not None:
            self._require(actor_id, "report.read")
        row = self._get_revision_row(revision_id)
        return {
            "revision_id": row["revision_id"],
            "agreement_id": row["agreement_id"],
            "candidate_id": row["candidate_id"],
            "revision_no": row["revision_no"],
            "state": row["state"],
            "effective_date": row["effective_date"],
            "expiry_date": row["expiry_date"],
            "signed_at": row["signed_at"],
            "superseded_at": row["superseded_at"],
            "terminated_at": row["terminated_at"],
            "licensor_party_id": row["licensor_party_id"],
            "licensee_party_id": row["licensee_party_id"],
            "scope": json.loads(row["scope_json"]),
            "exclusivity": row["exclusivity"],
            "sublicense_scope": row["sublicense_scope"],
            "upfront_amount": row["upfront_amount"],
            "royalty": json.loads(row["royalty_json"]) if row["royalty_json"] else None,
            "sublicense_income_share": row["sublicense_income_share"],
            "source_revision_id": row["source_revision_id"],
            "snapshot": json.loads(row["snapshot_json"]),
            "snapshot_hash": row["snapshot_hash"],
            "change_note": row["change_note"],
        }

    # --------------------------------------------------------------- 义务履行

    def fulfill_obligation(self, actor_id: str, revision_id: str, obligation_id: str,
                           note: str, evidence_ref: str | None = None) -> dict:
        self._require(actor_id, "obligation.write")
        row = self.db.execute(
            "SELECT * FROM obligations WHERE revision_id=? AND obligation_id=?",
            (revision_id, obligation_id),
        ).fetchone()
        if row is None:
            raise NotFound("义务不存在")
        if row["state"] != "open":
            raise InvalidState("义务已处理")
        with transaction(self.db):
            self.db.execute(
                "UPDATE obligations SET state='fulfilled', fulfilled_at=?, fulfilled_by=? "
                "WHERE obligation_uid=?",
                (utc_text(self.clock.now()), actor_id, row["obligation_uid"]),
            )
            self.db.execute(
                "INSERT INTO obligation_fulfillments(obligation_uid,note,evidence_ref,"
                "actor_id,created_at) VALUES(?,?,?,?,?)",
                (row["obligation_uid"], note, evidence_ref, actor_id, utc_text(self.clock.now())),
            )
        self._audit("obligation", row["obligation_uid"], "obligation.fulfilled", actor_id, {
            "revision_id": revision_id, "obligation_id": obligation_id,
            "evidence_ref": evidence_ref,
        })
        return self._obligation_dict(row["obligation_uid"])

    def waive_obligation(self, actor_id: str, revision_id: str, obligation_id: str, note: str) -> dict:
        self._require(actor_id, "agreement.sign")
        row = self.db.execute(
            "SELECT * FROM obligations WHERE revision_id=? AND obligation_id=?",
            (revision_id, obligation_id),
        ).fetchone()
        if row is None:
            raise NotFound("义务不存在")
        with transaction(self.db):
            self.db.execute(
                "UPDATE obligations SET state='waived', fulfilled_at=?, fulfilled_by=? "
                "WHERE obligation_uid=?",
                (utc_text(self.clock.now()), actor_id, row["obligation_uid"]),
            )
        self._audit("obligation", row["obligation_uid"], "obligation.waived", actor_id,
                    {"revision_id": revision_id, "obligation_id": obligation_id, "note": note})
        return self._obligation_dict(row["obligation_uid"])

    def _obligation_dict(self, uid: str) -> dict:
        row = self.db.execute("SELECT * FROM obligations WHERE obligation_uid=?", (uid,)).fetchone()
        return dict(row)

    # ------------------------------------------------------------------ 预测

    def create_projection(self, actor_id: str, revision_id: str,
                          assumptions: Mapping[str, Any], *, commit: bool = False) -> dict:
        permission = "projection.commit" if commit else "projection.write"
        self._require(actor_id, permission)
        revision = self._get_revision_row(revision_id)
        term = self._term_from_row(revision)
        lines = build_projection_lines(term, assumptions)
        lines_hash = digest({"lines": lines, "snapshot_hash": revision["snapshot_hash"]})
        projection_id = _new_id("proj")
        now_text = utc_text(self.clock.now())
        as_of = self.clock.today().isoformat()
        status = "committed" if commit else "draft"
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO projections(projection_id,revision_id,as_of_date,status,"
                "assumptions_json,assumptions_hash,snapshot_hash,lines_hash,created_by,"
                "created_at,committed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (projection_id, revision_id, as_of, status,
                 canonical_json(assumptions), digest(assumptions),
                 revision["snapshot_hash"], lines_hash, actor_id, now_text,
                 now_text if commit else None),
            )
            for seq, line in enumerate(lines):
                self.db.execute(
                    "INSERT INTO projection_lines(projection_id,line_seq,line_kind,ref_id,"
                    "period_label,probability,gross_amount,expected_amount,basis)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    (projection_id, seq, line["line_kind"], line["ref_id"],
                     line["period_label"], line["probability"], line["gross_amount"],
                     line["expected_amount"], line["basis"]),
                )
        self._audit("projection", projection_id,
                    "projection.committed" if commit else "projection.created", actor_id, {
                        "revision_id": revision_id, "lines_hash": lines_hash,
                        "snapshot_hash": revision["snapshot_hash"],
                    })
        return self.get_projection(actor_id, projection_id)

    def commit_projection(self, actor_id: str, projection_id: str) -> dict:
        self._require(actor_id, "projection.commit")
        row = self.db.execute("SELECT * FROM projections WHERE projection_id=?", (projection_id,)).fetchone()
        if row is None:
            raise NotFound("预测不存在")
        if row["status"] == "committed":
            raise InvalidState("预测已提交")
        with transaction(self.db):
            self.db.execute(
                "UPDATE projections SET status='committed', committed_at=? WHERE projection_id=?",
                (utc_text(self.clock.now()), projection_id),
            )
        self._audit("projection", projection_id, "projection.committed", actor_id, {})
        return self.get_projection(actor_id, projection_id)

    def _term_from_row(self, row) -> TermRevision:
        snapshot = json.loads(row["snapshot_json"])
        raw = dict(snapshot)
        raw["scope"] = snapshot["scope"]
        return TermRevision.from_dict(raw, row["revision_no"])

    def get_projection(self, actor_id: str, projection_id: str) -> dict:
        self._require(actor_id, "report.read")
        row = self.db.execute("SELECT * FROM projections WHERE projection_id=?", (projection_id,)).fetchone()
        if row is None:
            raise NotFound("预测不存在")
        lines = rows(self.db,
                     "SELECT line_kind,ref_id,period_label,probability,gross_amount,expected_amount,basis"
                     " FROM projection_lines WHERE projection_id=? ORDER BY line_seq",
                     (projection_id,))
        total = projection_total(lines)
        return {
            "projection_id": projection_id,
            "revision_id": row["revision_id"],
            "as_of_date": row["as_of_date"],
            "status": row["status"],
            "assumptions": json.loads(row["assumptions_json"]),
            "assumptions_hash": row["assumptions_hash"],
            "snapshot_hash": row["snapshot_hash"],
            "lines_hash": row["lines_hash"],
            "lines": lines,
            "expected_total": str(total),
            "created_by": row["created_by"],
            "created_at": row["created_at"],
            "committed_at": row["committed_at"],
        }

    # ---------------------------------------------------------- 真实事件确认

    def record_payment_event(self, actor_id: str, raw: Mapping[str, Any]) -> dict:
        """登记一笔真实付款/销售事件并展开授权链分配（幂等）。"""
        self._require(actor_id, "payment.write")
        revision_id = raw.get("revision_id")
        payment_kind = raw.get("payment_kind")
        if payment_kind not in PAYMENT_KIND_LABELS:
            raise ValidationFailed("payment_kind 必须是 upfront/milestone/royalty/sublicense")
        revision = self._get_revision_row(revision_id)
        if revision["state"] != "effective":
            raise InvalidState("只能针对当前有效版本确认收益；历史版本不得被新版本倒改")
        event_date = date_text(raw.get("event_date"), "event_date")
        if event_date < revision["effective_date"]:
            raise ValidationFailed("事件日期不能早于版本生效日")
        if revision["expiry_date"] and event_date > revision["expiry_date"]:
            raise InvalidState("版本已到期：续约谈判不会自动延长旧权利，请先签署新版本")
        idempotency_key = str(raw.get("idempotency_key", "")).strip()
        if not idempotency_key:
            raise ValidationFailed("idempotency_key 不能为空")

        duplicate = self.db.execute(
            "SELECT event_id FROM payment_events WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        if duplicate:
            existing = self.get_event(actor_id, duplicate["event_id"])
            existing["duplicate"] = True
            return existing

        gross = Decimal(str(raw.get("gross_amount", 0) or 0)).quantize(Decimal("0.01"))
        if gross < 0:
            raise ValidationFailed("gross_amount 不能为负")
        currency = str(raw.get("currency", "USD")).strip().upper()
        period_label = str(raw.get("period_label", event_date)).strip()

        basis_amount = None
        milestone_id = None
        if payment_kind == "royalty":
            basis_amount = Decimal(str(raw["basis_amount"])).quantize(Decimal("0.01"))
            if basis_amount < 0:
                raise ValidationFailed("basis_amount 不能为负")
            snapshot = json.loads(revision["snapshot_json"])
            if not snapshot.get("royalty"):
                raise ValidationFailed("该版本没有销售分成条款")
            if gross == 0:
                gross = (basis_amount * Decimal(snapshot["royalty"]["rate"])).quantize(Decimal("0.01"))
        elif payment_kind == "milestone":
            milestone_id = str(raw.get("milestone_id", "")).strip()
            snapshot = json.loads(revision["snapshot_json"])
            if not any(m["milestone_id"] == milestone_id for m in snapshot["milestones"]):
                raise ValidationFailed("milestone_id 不在版本快照中")
        elif payment_kind == "sublicense":
            if revision["source_revision_id"] is None:
                raise ValidationFailed("sublicense 事件只能登记在再许可版本上")
        if payment_kind in {"upfront", "milestone", "sublicense"} and gross == 0:
            raise ValidationFailed(f"{payment_kind} 事件的 gross_amount 必须大于 0")

        ancestor_rows: list = []
        # 直接上游链：沿 source_revision_id 逐级，取签署当时有效的来源版本
        chain = self._source_chain(revision, as_of=event_date)
        allocations = waterfall_allocations(
            revision, chain, payment_kind, gross, basis_amount, period_label,
        )

        event_id = _new_id("evt")
        created_at = utc_text(self.clock.now())
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO payment_events(event_id,agreement_id,revision_id,payment_kind,"
                "period_label,milestone_id,gross_amount,basis_amount,currency,event_date,"
                "idempotency_key,actor_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, revision["agreement_id"], revision_id, payment_kind,
                 period_label, milestone_id, str(gross),
                 str(basis_amount) if basis_amount is not None else None,
                 currency, event_date, idempotency_key, actor_id, created_at),
            )
            for alloc in allocations:
                self.db.execute(
                    "INSERT INTO payment_allocations(allocation_id,event_id,revision_id,"
                    "upstream_revision_id,recipient_party_id,flow_role,amount,currency,state,"
                    "basis_json,period_label,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (_new_id("alloc"), event_id, alloc["revision_id"],
                     alloc.get("upstream_revision_id"), alloc["recipient_party_id"],
                     alloc["flow_role"], alloc["amount"], currency, alloc["state"],
                     canonical_json(alloc["basis"]), period_label, created_at),
                )
        self._audit("payment_event", event_id, "payment.recorded", actor_id, {
            "revision_id": revision_id, "payment_kind": payment_kind,
            "gross_amount": str(gross), "idempotency_key": idempotency_key,
            "allocation_count": len(allocations),
        })
        return self.get_event(actor_id, event_id)

    def _source_chain(self, revision_row, *, as_of: str) -> list:
        """沿 source_revision_id 上溯的授权链，且每一环在 as_of 当时有效。"""
        chain = []
        current = revision_row
        seen = set()
        while current["source_revision_id"]:
            if current["source_revision_id"] in seen:
                raise InvalidState("授权链存在循环引用")
            seen.add(current["source_revision_id"])
            source = self.db.execute(
                "SELECT tr.*, a.candidate_id FROM term_revisions tr JOIN agreements a"
                " ON a.agreement_id=tr.agreement_id WHERE tr.revision_id=?",
                (current["source_revision_id"],),
            ).fetchone()
            if source is None:
                raise NotFound("来源版本缺失，授权链断裂")
            if source["effective_date"] > as_of:
                raise InvalidState("来源版本在事件时点尚未生效")
            if source["expiry_date"] and source["expiry_date"] < as_of:
                raise InvalidState("来源版本在事件时点已到期，收益不能回流至失效权利")
            chain.append(source)
            current = source
        return chain

    def _rights_chain(self, revision_id: str, *, as_of: str | None = None) -> list[dict]:
        revision = self._get_revision_row(revision_id)
        as_of = as_of or self.clock.today().isoformat()
        chain_rows = [dict(revision)]
        current = dict(revision)
        seen: set[str] = set()
        while current["source_revision_id"] and current["source_revision_id"] not in seen:
            seen.add(current["source_revision_id"])
            source = self.db.execute(
                "SELECT tr.*, a.candidate_id FROM term_revisions tr JOIN agreements a"
                " ON a.agreement_id=tr.agreement_id WHERE tr.revision_id=?",
                (current["source_revision_id"],),
            ).fetchone()
            if source is None:
                break
            chain_rows.append(dict(source))
            current = dict(source)
        result = []
        for row in chain_rows:
            result.append({
                "revision_id": row["revision_id"],
                "agreement_id": row["agreement_id"],
                "candidate_id": row["candidate_id"],
                "revision_no": row["revision_no"],
                "state": row["state"],
                "effective_date": row["effective_date"],
                "expiry_date": row["expiry_date"],
                "licensor_party_id": row["licensor_party_id"],
                "licensee_party_id": row["licensee_party_id"],
                "scope": json.loads(row["scope_json"]),
                "exclusivity": row["exclusivity"],
                "sublicense_scope": row["sublicense_scope"],
                "sublicense_income_share": row["sublicense_income_share"],
                "source_revision_id": row["source_revision_id"],
                "valid_at_as_of": (
                    row["state"] == "effective"
                    and row["effective_date"] <= as_of
                    and (not row["expiry_date"] or row["expiry_date"] >= as_of)
                ),
            })
        return result

    def get_event(self, actor_id: str, event_id: str) -> dict:
        self._require(actor_id, "report.read")
        row = self.db.execute("SELECT * FROM payment_events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise NotFound("付款事件不存在")
        allocations = rows(self.db,
                           "SELECT * FROM payment_allocations WHERE event_id=? ORDER BY allocation_id",
                           (event_id,))
        for alloc in allocations:
            alloc["basis"] = json.loads(alloc.pop("basis_json"))
        result = dict(row)
        result["allocations"] = allocations
        return result

    # ------------------------------------------------------------- 结算与争议

    def settle_allocation(self, actor_id: str, allocation_id: str, settlement_ref: str) -> dict:
        self._require(actor_id, "payment.settle")
        row = self.db.execute("SELECT * FROM payment_allocations WHERE allocation_id=?",
                              (allocation_id,)).fetchone()
        if row is None:
            raise NotFound("分配行不存在")
        if row["state"] == "voided":
            raise InvalidState("已冲销的份额不能结算")
        if row["state"] == "held":
            raise InvalidState("争议中的份额不能结算")
        if row["state"] == "settled":
            raise InvalidState("份额已经结算，已结算金额不可修改")
        with transaction(self.db):
            self.db.execute(
                "UPDATE payment_allocations SET state='settled', settled_at=?, settlement_ref=?"
                " WHERE allocation_id=?",
                (utc_text(self.clock.now()), settlement_ref, allocation_id),
            )
        self._audit("allocation", allocation_id, "allocation.settled", actor_id,
                    {"settlement_ref": settlement_ref, "amount": row["amount"]})
        return self._allocation_dict(allocation_id)

    def open_dispute(self, actor_id: str, event_id: str, allocation_ids: list[str], reason: str) -> dict:
        """争议只冻结涉及的份额（pending -> held），其余份额照常结算。"""
        self._require(actor_id, "dispute.write")
        event = self.db.execute("SELECT * FROM payment_events WHERE event_id=?", (event_id,)).fetchone()
        if event is None:
            raise NotFound("付款事件不存在")
        if not allocation_ids:
            raise ValidationFailed("争议至少涉及一个分配份额")
        dispute_id = _new_id("disp")
        frozen: list[str] = []
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO disputes(dispute_id,event_id,reason,status,created_by,created_at)"
                " VALUES(?,?,?, 'open', ?,?)",
                (dispute_id, event_id, reason, actor_id, utc_text(self.clock.now())),
            )
            for allocation_id in allocation_ids:
                alloc = self.db.execute(
                    "SELECT * FROM payment_allocations WHERE allocation_id=? AND event_id=?",
                    (allocation_id, event_id),
                ).fetchone()
                if alloc is None:
                    raise NotFound(f"分配份额 {allocation_id} 不属于该事件")
                if alloc["state"] == "settled":
                    raise InvalidState(f"份额 {allocation_id} 已结算，不能冻结；应通过调整行处理")
                if alloc["state"] == "voided":
                    raise InvalidState(f"份额 {allocation_id} 已冲销")
                self.db.execute(
                    "INSERT INTO dispute_allocations(dispute_id,allocation_id) VALUES(?,?)",
                    (dispute_id, allocation_id),
                )
                if alloc["state"] == "pending":
                    self.db.execute(
                        "UPDATE payment_allocations SET state='held' WHERE allocation_id=?",
                        (allocation_id,),
                    )
                    frozen.append(allocation_id)
        self._audit("dispute", dispute_id, "dispute.opened", actor_id,
                    {"event_id": event_id, "allocation_ids": allocation_ids,
                     "frozen": frozen, "reason": reason})
        return self.get_dispute(actor_id, dispute_id)

    def resolve_dispute(self, actor_id: str, dispute_id: str, outcome: str,
                        adjustments: Mapping[str, str] | None = None, note: str = "") -> dict:
        """裁决：release 解冻为 pending；uphold 冲销相关 held 份额。

        已结算金额永不被修改；如需差额，使用 adjustments 传入
        {allocation_id: 新金额} 生成 adjustment 行（原行保留为 voided/留痕）。
        """
        self._require(actor_id, "dispute.resolve")
        dispute = self.db.execute("SELECT * FROM disputes WHERE dispute_id=?", (dispute_id,)).fetchone()
        if dispute is None:
            raise NotFound("争议不存在")
        if dispute["status"] != "open":
            raise InvalidState("争议已裁决")
        if outcome not in {"release", "uphold"}:
            raise ValidationFailed("outcome 必须是 release 或 uphold")
        linked = rows(self.db,
                      "SELECT a.* FROM payment_allocations a JOIN dispute_allocations d"
                      " ON d.allocation_id=a.allocation_id WHERE d.dispute_id=?",
                      (dispute_id,))
        resolution = {"outcome": outcome, "note": note, "adjustments": []}
        with transaction(self.db):
            for alloc in linked:
                if outcome == "release" and alloc["state"] == "held":
                    self.db.execute(
                        "UPDATE payment_allocations SET state='pending' WHERE allocation_id=?",
                        (alloc["allocation_id"],),
                    )
                elif outcome == "uphold" and alloc["state"] == "held":
                    self.db.execute(
                        "UPDATE payment_allocations SET state='voided' WHERE allocation_id=?",
                        (alloc["allocation_id"],),
                    )
            adjustments = adjustments or {}
            for allocation_id, new_amount_text in adjustments.items():
                source = self.db.execute(
                    "SELECT * FROM payment_allocations WHERE allocation_id=?", (allocation_id,)
                ).fetchone()
                if source is None:
                    raise NotFound(f"调整来源份额 {allocation_id} 不存在")
                new_amount = Decimal(str(new_amount_text)).quantize(Decimal("0.01"))
                if new_amount < 0:
                    raise ValidationFailed("调整金额不能为负")
                adjustment_id = _new_id("alloc")
                self.db.execute(
                    "INSERT INTO payment_allocations(allocation_id,event_id,revision_id,"
                    "upstream_revision_id,recipient_party_id,flow_role,amount,currency,state,"
                    "basis_json,period_label,adjusted_allocation_id,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (adjustment_id, source["event_id"], source["revision_id"],
                     source["upstream_revision_id"], source["recipient_party_id"],
                     "adjustment", str(new_amount), source["currency"], "pending",
                     canonical_json({
                         "rule": "dispute_adjustment",
                         "dispute_id": dispute_id,
                         "original_allocation_id": allocation_id,
                         "original_amount": source["amount"],
                         "adjusted_amount": str(new_amount),
                     }),
                     source["period_label"], allocation_id, utc_text(self.clock.now())),
                )
                resolution["adjustments"].append(
                    {"allocation_id": adjustment_id, "amount": str(new_amount)})
            self.db.execute(
                "UPDATE disputes SET status='resolved', resolved_at=?, resolution_json=?"
                " WHERE dispute_id=?",
                (utc_text(self.clock.now()), canonical_json(resolution), dispute_id),
            )
        self._audit("dispute", dispute_id, "dispute.resolved", actor_id, resolution)
        return self.get_dispute(actor_id, dispute_id)

    def get_dispute(self, actor_id: str, dispute_id: str) -> dict:
        self._require(actor_id, "report.read")
        row = self.db.execute("SELECT * FROM disputes WHERE dispute_id=?", (dispute_id,)).fetchone()
        if row is None:
            raise NotFound("争议不存在")
        result = dict(row)
        result["resolution"] = json.loads(row["resolution_json"]) if row["resolution_json"] else None
        result.pop("resolution_json", None)
        result["allocations"] = rows(self.db,
            "SELECT a.* FROM payment_allocations a JOIN dispute_allocations d"
            " ON d.allocation_id=a.allocation_id WHERE d.dispute_id=?", (dispute_id,))
        for alloc in result["allocations"]:
            alloc["basis"] = json.loads(alloc.pop("basis_json"))
        return result

    def _allocation_dict(self, allocation_id: str) -> dict:
        row = self.db.execute("SELECT * FROM payment_allocations WHERE allocation_id=?",
                              (allocation_id,)).fetchone()
        result = dict(row)
        result["basis"] = json.loads(result.pop("basis_json"))
        return result

    # ------------------------------------------------------------- 追溯视图

    def rights_chain(self, actor_id: str, revision_id: str) -> dict:
        self._require(actor_id, "report.read")
        as_of = self.clock.today().isoformat()
        chain = self._rights_chain(revision_id, as_of=as_of)
        return {"as_of_date": as_of, "chain": chain}

    def territory_trace(self, actor_id: str, candidate_id: str | None = None,
                        territory: str | None = None) -> list[dict]:
        """从任一地区或候选药追溯所有涉及的授权版本。"""
        self._require(actor_id, "report.read")
        query = (
            "SELECT tr.*, a.candidate_id FROM term_revisions tr JOIN agreements a"
            " ON a.agreement_id=tr.agreement_id WHERE 1=1"
        )
        args: list = []
        if candidate_id:
            query += " AND a.candidate_id=?"
            args.append(candidate_id)
        rows_ = self.db.execute(query, args).fetchall()
        result = []
        for row in rows_:
            scope = json.loads(row["scope_json"])
            if territory and territory not in scope["territories"]:
                continue
            result.append({
                "revision_id": row["revision_id"], "agreement_id": row["agreement_id"],
                "candidate_id": row["candidate_id"], "revision_no": row["revision_no"],
                "state": row["state"], "effective_date": row["effective_date"],
                "expiry_date": row["expiry_date"],
                "scope": scope, "exclusivity": row["exclusivity"],
                "source_revision_id": row["source_revision_id"],
            })
        return result

    def open_obligations(self, actor_id: str, *, candidate_id: str | None = None,
                         territory: str | None = None, include_overdue: bool = True) -> dict:
        """未满足义务（及逾期标记），可按候选药/地区过滤。"""
        self._require(actor_id, "report.read")
        today = self.clock.today().isoformat()
        query = (
            "SELECT o.*, tr.scope_json, tr.state AS revision_state,"
            " tr.effective_date AS revision_effective_date, a.candidate_id"
            " FROM obligations o JOIN term_revisions tr ON tr.revision_id=o.revision_id"
            " JOIN agreements a ON a.agreement_id=o.agreement_id"
            " WHERE o.state='open' AND tr.state IN ('effective','superseded')"
        )
        args: list = []
        if candidate_id:
            query += " AND a.candidate_id=?"
            args.append(candidate_id)
        rows_ = self.db.execute(query, args).fetchall()
        items = []
        for row in rows_:
            scope = json.loads(row["scope_json"])
            if territory and territory not in scope["territories"]:
                continue
            overdue = bool(
                include_overdue
                and row["due_date"]
                and row["due_date"] < today
                and row["revision_state"] == "effective"
                and row["revision_effective_date"] <= row["due_date"]
            )
            items.append({
                "obligation_uid": row["obligation_uid"],
                "revision_id": row["revision_id"],
                "agreement_id": row["agreement_id"],
                "candidate_id": row["candidate_id"],
                "obligation_id": row["obligation_id"],
                "kind": row["kind"],
                "description": row["description"],
                "due_date": row["due_date"],
                "due_event": row["due_event"],
                "owner_party_id": row["owner_party_id"],
                "state": row["state"],
                "overdue": overdue,
                "scope": scope,
            })
        return {"as_of_date": today, "overdue_count": sum(1 for i in items if i["overdue"]),
                "obligations": items}

    def allocation_basis(self, actor_id: str, *, recipient_party_id: str | None = None,
                         candidate_id: str | None = None) -> list[dict]:
        """每笔分配的依据：金额、公式、来源版本与事件。"""
        self._require(actor_id, "report.read")
        query = (
            "SELECT al.*, e.payment_kind, e.event_date, e.currency AS event_currency,"
            " a.candidate_id FROM payment_allocations al JOIN payment_events e"
            " ON e.event_id=al.event_id JOIN agreements a ON a.agreement_id=e.agreement_id WHERE 1=1"
        )
        args: list = []
        if recipient_party_id:
            query += " AND al.recipient_party_id=?"
            args.append(recipient_party_id)
        if candidate_id:
            query += " AND a.candidate_id=?"
            args.append(candidate_id)
        query += " ORDER BY e.event_date, al.allocation_id"
        rows_ = self.db.execute(query, args).fetchall()
        result = []
        for row in rows_:
            result.append({
                "allocation_id": row["allocation_id"],
                "event_id": row["event_id"],
                "candidate_id": row["candidate_id"],
                "payment_kind": row["payment_kind"],
                "event_date": row["event_date"],
                "revision_id": row["revision_id"],
                "upstream_revision_id": row["upstream_revision_id"],
                "recipient_party_id": row["recipient_party_id"],
                "flow_role": row["flow_role"],
                "amount": row["amount"],
                "currency": row["currency"],
                "state": row["state"],
                "period_label": row["period_label"],
                "settlement_ref": row["settlement_ref"],
                "adjusted_allocation_id": row["adjusted_allocation_id"],
                "basis": json.loads(row["basis_json"]),
            })
        return result
