"""版本化海外授权与收益台账的事务用例。

设计要点：
- 条款以“协议 → 不可变修订版本（内容哈希）”组织；新版本只使旧版本状态变为 superseded，
  旧行永不删除，收益事件与分配永久引用事件发生当日“当时有效”的版本。
- 已结算（settlements 有流水）的分配金额被锁定：不允许冲正、冻结或任何改写。
- 预测只依赖假设集哈希 + 当时有效版本快照哈希，相同输入返回同一结果（可复算）。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from decimal import Decimal
from typing import Any, Mapping, Sequence

from .clock import SystemClock, today, utc_text
from .domain import (
    AssumptionSet,
    VersionContent,
    canonical_json,
    money_text,
    split_amount,
    project_inflows,
)
from .errors import Conflict, Forbidden, InvalidState, NotFound, ReviewBlocked, ValidationFailed
from .review import ReviewIssue, SourceLink, VersionContext, blocking, review_version
from .storage import initialize, transaction

ROLE_PERMISSIONS = {
    "bd_manager": {
        "catalog.write", "agreement.write", "version.draft", "review.run",
        "forecast.run", "chain.read", "report.read",
    },
    "dealmaker": {
        "catalog.write", "agreement.write", "version.draft", "version.sign",
        "version.discard", "review.run", "forecast.run", "chain.read", "report.read",
    },
    "finance": {
        "assumptions.write", "event.write", "dispute.write", "distribution.confirm",
        "distribution.reverse", "settlement.write", "forecast.run",
        "review.run", "chain.read", "report.read",
    },
    "auditor": {"audit.read", "chain.read", "report.read"},
}

REVIEW_STATES = ("proposed", "effective")


class LicensingService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ------------------------------------------------------------ 基础

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _today(self) -> str:
        return today(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM lic_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS.get(user["role"], set()):
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM lic_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO lic_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (entity_type, entity_id, event_type, actor_id, canonical_json(payload),
             previous_hash, event_hash, body["created_at"]),
        )

    def create_user(self, actor_id: str, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        # 用户引导不设权限门槛；API 层与既有子系统一致由部署方控制入口。
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO lic_users(user_id,display_name,role,created_at) VALUES(?,?,?,?)",
                    (user_id.strip(), display_name.strip(), role, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role}

    def register_candidate(self, actor_id: str, candidate_id: str, name: str, internal_code: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO candidates(candidate_id,name,internal_code,created_at) VALUES(?,?,?,?)",
                    (candidate_id, name, internal_code, self._now()),
                )
                self._audit("candidate", candidate_id, "candidate.registered", actor_id, {"name": name})
        except sqlite3.IntegrityError as exc:
            raise Conflict("候选药编号已经存在") from exc
        return {"candidate_id": candidate_id, "name": name}

    def register_party(self, actor_id: str, party_id: str, name: str, kind: str) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        if kind not in {"licensor", "licensee", "partner"}:
            raise ValidationFailed("kind 必须是 licensor、licensee 或 partner")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO counterparties(party_id,name,kind,created_at) VALUES(?,?,?,?)",
                    (party_id, name, kind, self._now()),
                )
                self._audit("party", party_id, "party.registered", actor_id, {"name": name, "kind": kind})
        except sqlite3.IntegrityError as exc:
            raise Conflict("交易方编号已经存在") from exc
        return {"party_id": party_id, "name": name, "kind": kind}

    # ------------------------------------------------------------ 协议与版本

    def create_agreement(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "agreement.write")
        agreement_id = str(raw["agreement_id"]).strip()
        agreement_no = str(raw["agreement_no"]).strip()
        candidate_id = str(raw["candidate_id"]).strip()
        direction = str(raw["direction"]).strip()
        if direction not in {"inbound", "outbound"}:
            raise ValidationFailed("direction 必须是 inbound 或 outbound")
        licensor = str(raw["licensor_party_id"]).strip()
        licensee = str(raw["licensee_party_id"]).strip()
        parent = (raw.get("parent_agreement_id") or None)
        renewal_of = (raw.get("renewal_of_agreement_id") or None)
        auto_renew = 1 if raw.get("auto_renew") else 0
        with transaction(self.connection, immediate=True):
            self._must_exist("candidates", "candidate_id", candidate_id, "候选药")
            self._must_exist("counterparties", "party_id", licensor, "授权方")
            self._must_exist("counterparties", "party_id", licensee, "被授权方")
            if parent is not None:
                self._must_exist("agreements", "agreement_id", parent, "上游协议")
            if renewal_of is not None:
                self._must_exist("agreements", "agreement_id", renewal_of, "续约原协议")
            try:
                self.connection.execute(
                    "INSERT INTO agreements(agreement_id,agreement_no,candidate_id,direction,"
                    "licensor_party_id,licensee_party_id,parent_agreement_id,renewal_of_agreement_id,"
                    "auto_renew,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (agreement_id, agreement_no, candidate_id, direction, licensor, licensee,
                     parent, renewal_of, auto_renew, actor_id, self._now()),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("协议编号/合同号冲突或引用不存在") from exc
            self._audit("agreement", agreement_id, "agreement.created", actor_id, {
                "candidate_id": candidate_id, "direction": direction,
                "parent_agreement_id": parent, "renewal_of_agreement_id": renewal_of,
                "auto_renew": bool(auto_renew),
            })
        return self.agreement(actor_id, agreement_id)

    def _must_exist(self, table: str, key: str, value: str, label: str) -> None:
        if self.connection.execute(f"SELECT 1 FROM {table} WHERE {key}=?", (value,)).fetchone() is None:
            raise ValidationFailed(f"{label} {value} 不存在")

    def agreement(self, actor_id: str, agreement_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        row = self.connection.execute("SELECT * FROM agreements WHERE agreement_id=?", (agreement_id,)).fetchone()
        if row is None:
            raise NotFound("协议不存在")
        result = dict(row)
        result["in_force_on"] = self._today()
        result["in_force"] = self._effective_version_row(agreement_id, self._today()) is not None
        return result

    def _load_content(self, version_row: sqlite3.Row) -> VersionContent:
        return VersionContent.from_dict(json.loads(version_row["content_json"]))

    def _latest_version_row(self, agreement_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM agreement_versions WHERE agreement_id=? ORDER BY seq DESC LIMIT 1",
            (agreement_id,),
        ).fetchone()

    def _effective_version_row(self, agreement_id: str, on_date: str) -> sqlite3.Row | None:
        """解析某一日期“当时在效”的版本（严格落在生效窗口内）。

        已签署版本包括 effective 与 superseded：旧版本被新版本取代后，其历史期间
        仍然是该期间事件与分配的权利来源，因此按生效日就近选取，而不只看当前状态。
        仅用于收益事件的权利来源核验。
        """
        return self.connection.execute(
            "SELECT * FROM agreement_versions WHERE agreement_id=? "
            "AND state IN ('effective','superseded') "
            "AND effective_date<=? AND (term_end_date IS NULL OR term_end_date>=?) "
            "ORDER BY effective_date DESC, seq DESC LIMIT 1",
            (agreement_id, on_date, on_date),
        ).fetchone()

    def _signed_version_row(self, agreement_id: str) -> sqlite3.Row | None:
        """当前已签署（现行）版本，允许生效日在未来；义务跟踪与条款展示使用。"""
        return self.connection.execute(
            "SELECT * FROM agreement_versions WHERE agreement_id=? AND state='effective' "
            "ORDER BY seq DESC LIMIT 1",
            (agreement_id,),
        ).fetchone()

    def draft_version(self, actor_id: str, agreement_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "version.draft")
        agreement = self.connection.execute(
            "SELECT * FROM agreements WHERE agreement_id=?", (agreement_id,)
        ).fetchone()
        if agreement is None:
            raise NotFound("协议不存在")
        try:
            content = VersionContent.from_dict(raw)
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        with transaction(self.connection, immediate=True):
            latest = self._latest_version_row(agreement_id)
            active_latest = self.connection.execute(
                "SELECT * FROM agreement_versions WHERE agreement_id=? AND state<>'discarded' "
                "ORDER BY seq DESC LIMIT 1",
                (agreement_id,),
            ).fetchone()
            if active_latest is not None and active_latest["state"] == "proposed":
                raise Conflict("已有在谈修订版本，请先签署评审或撤回")
            seq = 1 if latest is None else latest["seq"] + 1
            parent_version_id = None if active_latest is None else active_latest["version_id"]
            version_id = f"{agreement_id}:v{seq}"
            payload = content.to_dict()
            sha = content.content_sha256()
            self.connection.execute(
                "INSERT INTO agreement_versions(version_id,agreement_id,seq,parent_version_id,"
                "effective_date,term_end_date,currency,content_json,content_sha256,created_by,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (version_id, agreement_id, seq, parent_version_id, content.effective_date,
                 content.term_end_date, content.currency, canonical_json(payload), sha,
                 actor_id, self._now()),
            )
            self._audit("version", version_id, "version.drafted", actor_id, {
                "agreement_id": agreement_id, "seq": seq, "content_sha256": sha,
            })
        return self.version(actor_id, version_id)

    def discard_version(self, actor_id: str, version_id: str) -> dict[str, Any]:
        self._require(actor_id, "version.discard")
        with transaction(self.connection, immediate=True):
            row = self._version_row(version_id)
            if row["state"] != "proposed":
                raise InvalidState("只有在谈版本可以撤回")
            self.connection.execute(
                "UPDATE agreement_versions SET state='discarded' WHERE version_id=?", (version_id,)
            )
            self._audit("version", version_id, "version.discarded", actor_id, {})
        return {"version_id": version_id, "state": "discarded"}

    def _version_row(self, version_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM agreement_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if row is None:
            raise NotFound("修订版本不存在")
        return row

    def version(self, actor_id: str, version_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        row = self._version_row(version_id)
        return self._version_view(row)

    def _version_view(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "version_id": row["version_id"],
            "agreement_id": row["agreement_id"],
            "seq": row["seq"],
            "parent_version_id": row["parent_version_id"],
            "state": row["state"],
            "effective_date": row["effective_date"],
            "term_end_date": row["term_end_date"],
            "currency": row["currency"],
            "content_sha256": row["content_sha256"],
            "signed_at": row["signed_at"],
            "content": json.loads(row["content_json"]),
        }

    def list_versions(self, actor_id: str, agreement_id: str) -> list[dict[str, Any]]:
        self._require(actor_id, "report.read")
        rows = self.connection.execute(
            "SELECT * FROM agreement_versions WHERE agreement_id=? ORDER BY seq",
            (agreement_id,),
        ).fetchall()
        return [self._version_view(row) for row in rows]

    # ------------------------------------------------------------ 签署前评审

    def _gather_review_inputs(
        self, agreement_row: sqlite3.Row, version_row: sqlite3.Row, content: VersionContent
    ) -> tuple[list[VersionContext], list[SourceLink], bool, str | None, list[ReviewIssue]]:
        candidate_id = agreement_row["candidate_id"]
        # 先沿 parent_agreement_id 求祖先集合：上游授权与本协议范围重叠是合法派生，
        # 不应作为范围冲突比对对象，但仍是权利来源核验对象。
        ancestors: set[str] = set()
        cursor_id = agreement_row["parent_agreement_id"]
        while cursor_id is not None and cursor_id not in ancestors:
            ancestors.add(cursor_id)
            row = self.connection.execute(
                "SELECT parent_agreement_id FROM agreements WHERE agreement_id=?", (cursor_id,)
            ).fetchone()
            cursor_id = row["parent_agreement_id"] if row else None
        # 续签链上的前序协议同样豁免范围冲突：续签与旧协议范围重叠是预期行为，
        # 时间衔接由 renewal_overlaps_prior_term 规则单独约束。
        renewed: set[str] = set()
        cursor_id = agreement_row["renewal_of_agreement_id"]
        while cursor_id is not None and cursor_id not in renewed:
            renewed.add(cursor_id)
            row = self.connection.execute(
                "SELECT renewal_of_agreement_id FROM agreements WHERE agreement_id=?", (cursor_id,)
            ).fetchone()
            cursor_id = row["renewal_of_agreement_id"] if row else None
        exclude = ancestors | renewed
        siblings: list[VersionContext] = []
        # 同一兄弟协议只取最新未废弃版本：旧版本会被新版本取代，协议的当前意图以最新版为准，
        # 同时避免 v1/v2 对同一重叠重复告警。
        rows = self.connection.execute(
            "SELECT v.* FROM agreement_versions v "
            "JOIN (SELECT agreement_id, MAX(seq) AS max_seq FROM agreement_versions "
            "WHERE state<>'discarded' GROUP BY agreement_id) latest "
            "ON latest.agreement_id=v.agreement_id AND latest.max_seq=v.seq "
            "JOIN agreements a ON a.agreement_id=v.agreement_id "
            "WHERE a.candidate_id=? AND v.version_id<>? AND a.agreement_id<>? ORDER BY v.version_id",
            (candidate_id, version_row["version_id"], agreement_row["agreement_id"]),
        ).fetchall()
        for row in rows:
            if row["agreement_id"] in exclude:
                continue
            siblings.append(VersionContext(
                row["agreement_id"], row["version_id"], row["state"], self._load_content(row),
            ))
        # 沿 parent_agreement_id 向上收集权利来源链，取候选版本生效日当日有效的来源版本。
        sources: list[SourceLink] = []
        extra: list[ReviewIssue] = []
        seen: set[str] = set()
        current = agreement_row["parent_agreement_id"]
        while current is not None and current not in seen:
            seen.add(current)
            source_agreement = self.connection.execute(
                "SELECT * FROM agreements WHERE agreement_id=?", (current,)
            ).fetchone()
            source_row = self._effective_version_row(current, content.effective_date)
            if source_row is None:
                extra.append(ReviewIssue(
                    "source_not_effective", "blocking",
                    f"上游权利来源协议 {current} 在 {content.effective_date} 没有生效版本，权利链断裂",
                    {"source_agreement_id": current, "on_date": content.effective_date},
                ))
            else:
                sources.append(SourceLink(current, source_row["version_id"],
                                          source_agreement["direction"], self._load_content(source_row)))
            current = source_agreement["parent_agreement_id"] if source_agreement else None
        is_renewal = agreement_row["renewal_of_agreement_id"] is not None
        previous_term_end = None
        if is_renewal:
            prior = self.connection.execute(
                "SELECT max(term_end_date) AS end_date FROM agreement_versions "
                "WHERE agreement_id=? AND state='effective'",
                (agreement_row["renewal_of_agreement_id"],),
            ).fetchone()
            previous_term_end = prior["end_date"] if prior else None
        return siblings, sources, is_renewal, previous_term_end, extra

    def run_review(
        self, actor_id: str, version_id: str, waived_obligation_ids: Sequence[str] | None = None
    ) -> dict[str, Any]:
        self._require(actor_id, "review.run")
        version_row = self._version_row(version_id)
        agreement_row = self.connection.execute(
            "SELECT * FROM agreements WHERE agreement_id=?", (version_row["agreement_id"],)
        ).fetchone()
        content = self._load_content(version_row)
        siblings, sources, is_renewal, previous_term_end, extra = self._gather_review_inputs(
            agreement_row, version_row, content
        )
        issues = extra + review_version(
            content, version_id, siblings, sources,
            is_renewal=is_renewal, previous_term_end=previous_term_end,
            waived_obligation_ids=frozenset(waived_obligation_ids or ()),
        )
        report = {
            "version_id": version_id,
            "agreement_id": version_row["agreement_id"],
            "reviewed_at": self._now(),
            "blocking_count": len(blocking(issues)),
            "issues": [issue.to_dict() for issue in issues],
        }
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO version_reviews(agreement_id,version_id,blocking_count,report_json,"
                "reviewed_by,created_at) VALUES(?,?,?,?,?,?)",
                (version_row["agreement_id"], version_id, report["blocking_count"],
                 canonical_json(report), actor_id, self._now()),
            )
            self._audit("version", version_id, "version.reviewed", actor_id,
                        {"blocking_count": report["blocking_count"]})
        return report

    def sign_version(
        self, actor_id: str, version_id: str, waived_obligation_ids: Sequence[str] | None = None
    ) -> dict[str, Any]:
        self._require(actor_id, "version.sign")
        version_row = self._version_row(version_id)
        if version_row["state"] != "proposed":
            raise InvalidState("只有在谈版本可以签署")
        agreement_row = self.connection.execute(
            "SELECT * FROM agreements WHERE agreement_id=?", (version_row["agreement_id"],)
        ).fetchone()
        content = self._load_content(version_row)
        siblings, sources, is_renewal, previous_term_end, extra = self._gather_review_inputs(
            agreement_row, version_row, content
        )
        issues = extra + review_version(
            content, version_id, siblings, sources,
            is_renewal=is_renewal, previous_term_end=previous_term_end,
            waived_obligation_ids=frozenset(waived_obligation_ids or ()),
        )
        blockers = blocking(issues)
        report = {
            "version_id": version_id,
            "agreement_id": version_row["agreement_id"],
            "reviewed_at": self._now(),
            "blocking_count": len(blockers),
            "issues": [issue.to_dict() for issue in issues],
        }
        # 评审报告先在独立事务落库：即使因阻断项拒签，评审痕迹也不被回滚。
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "INSERT INTO version_reviews(agreement_id,version_id,blocking_count,report_json,"
                "reviewed_by,created_at) VALUES(?,?,?,?,?,?)",
                (version_row["agreement_id"], version_id, len(blockers),
                 canonical_json(report), actor_id, self._now()),
            )
            if blockers:
                self._audit("version", version_id, "version.sign_blocked", actor_id,
                            {"blocking_count": len(blockers)})
            else:
                self._audit("version", version_id, "version.reviewed_at_sign", actor_id, {})
        if blockers:
            raise ReviewBlocked(f"存在 {len(blockers)} 项阻断性评审问题，不能签署")
        with transaction(self.connection, immediate=True):
            # 重新确认状态，防止评审落库与签署之间被并发改动。
            fresh = self.connection.execute(
                "SELECT state FROM agreement_versions WHERE version_id=?", (version_id,)
            ).fetchone()
            if fresh["state"] != "proposed":
                raise InvalidState("版本状态已变化，请重新评审后再签署")
            self.connection.execute(
                "UPDATE agreement_versions SET state='superseded',superseded_by_version_id=? "
                "WHERE agreement_id=? AND state='effective'",
                (version_id, version_row["agreement_id"]),
            )
            signed_at = self._now()
            self.connection.execute(
                "UPDATE agreement_versions SET state='effective',signed_at=? WHERE version_id=?",
                (signed_at, version_id),
            )
            self.connection.execute(
                "UPDATE agreements SET state='active' WHERE agreement_id=?",
                (version_row["agreement_id"],),
            )
            # 义务按 (协议,义务编号) 跨版本沿用既有履行状态，新编号义务初始化为 open。
            for obligation in content.obligations:
                self.connection.execute(
                    "INSERT INTO obligation_status(agreement_id,obligation_id,version_id,state,"
                    "updated_by,updated_at) SELECT ?,?,?, 'open', ?, ? "
                    "WHERE NOT EXISTS(SELECT 1 FROM obligation_status WHERE agreement_id=? AND obligation_id=?)",
                    (version_row["agreement_id"], obligation.obligation_id, version_id,
                     actor_id, signed_at, version_row["agreement_id"], obligation.obligation_id),
                )
            self._instantiate_upfronts(
                version_row["agreement_id"], version_id, content, actor_id, signed_at
            )
            self._audit("version", version_id, "version.signed", actor_id, {
                "content_sha256": version_row["content_sha256"],
                "source_versions": [s.version_id for s in sources],
            })
        return self.version(actor_id, version_id)

    def _instantiate_upfronts(
        self, agreement_id: str, version_id: str, content: VersionContent,
        actor_id: str, signed_at: str,
    ) -> None:
        """签署即确立首付款：生成应收事件与待确认分配，之后随真实收款逐期确认/结算。

        里程碑与销售分成不在这里实例化——它们由真实触发事件逐期登记。
        """
        for term in content.terms:
            if term.kind != "upfront" or term.due_date is None:
                continue
            event_id = f"EV-{version_id}-{term.term_id}"
            idem = f"upfront:{version_id}:{term.term_id}"
            exists = self.connection.execute(
                "SELECT 1 FROM revenue_events WHERE idempotency_key=?", (idem,)
            ).fetchone()
            if exists:
                continue
            self.connection.execute(
                "INSERT INTO revenue_events(event_id,agreement_id,version_id,term_id,kind,period,"
                "region,indication,face_amount,currency,event_date,source_note,idempotency_key,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, agreement_id, version_id, term.term_id, "upfront",
                 term.due_date[:7], None, None, money_text(term.amount), term.currency,
                 term.due_date, "签署版本确立的首付款应收", idem, actor_id, signed_at),
            )
            amounts = split_amount(term.amount, term.shares)
            for share, amount in zip(term.shares, amounts):
                self.connection.execute(
                    "INSERT INTO distributions(distribution_id,event_id,agreement_id,version_id,"
                    "term_id,period,recipient_party_id,share_bp,amount,currency,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (f"dist-{uuid.uuid4().hex[:16]}", event_id, agreement_id, version_id,
                     term.term_id, term.due_date[:7], share.recipient_party_id, share.share_bp,
                     money_text(amount), term.currency, signed_at),
                )

    def terminate_agreement(self, actor_id: str, agreement_id: str, note: str) -> dict[str, Any]:
        self._require(actor_id, "version.sign")
        if not note.strip():
            raise ValidationFailed("终止说明不能为空")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT state FROM agreements WHERE agreement_id=?", (agreement_id,)
            ).fetchone()
            if row is None:
                raise NotFound("协议不存在")
            if row["state"] != "active":
                raise InvalidState("只有生效中协议可以终止")
            self.connection.execute(
                "UPDATE agreements SET state='terminated' WHERE agreement_id=?", (agreement_id,)
            )
            self._audit("agreement", agreement_id, "agreement.terminated", actor_id, {"note": note})
        return {"agreement_id": agreement_id, "state": "terminated"}

    # ------------------------------------------------------------ 义务履行

    def satisfy_obligation(self, actor_id: str, agreement_id: str, obligation_id: str, note: str) -> dict[str, Any]:
        self._require(actor_id, "agreement.write")
        with transaction(self.connection, immediate=True):
            current = self.connection.execute(
                "SELECT version_id,state FROM obligation_status WHERE agreement_id=? AND obligation_id=?",
                (agreement_id, obligation_id),
            ).fetchone()
            if current is None:
                raise NotFound("义务在当前协议中不存在")
            signed = self._signed_version_row(agreement_id)
            version_id = signed["version_id"] if signed else current["version_id"]
            self.connection.execute(
                "UPDATE obligation_status SET state='satisfied',evidence_note=?,version_id=?,"
                "updated_by=?,updated_at=? WHERE agreement_id=? AND obligation_id=?",
                (note, version_id, actor_id, self._now(), agreement_id, obligation_id),
            )
            self._audit("obligation", f"{agreement_id}/{obligation_id}", "obligation.satisfied",
                        actor_id, {"note": note})
        return {"agreement_id": agreement_id, "obligation_id": obligation_id, "state": "satisfied"}

    def list_obligations(self, actor_id: str, agreement_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        version_row = self._signed_version_row(agreement_id)
        if version_row is None:
            raise NotFound("协议当前没有已签署版本")
        content = self._load_content(version_row)
        result = []
        for obligation in content.obligations:
            status = self.connection.execute(
                "SELECT * FROM obligation_status WHERE agreement_id=? AND obligation_id=?",
                (agreement_id, obligation.obligation_id),
            ).fetchone()
            state = "open" if status is None else status["state"]
            result.append({
                **obligation.to_dict(),
                "state": state,
                "evidence_note": None if status is None else status["evidence_note"],
                "overdue": state == "open" and obligation.required
                           and obligation.due_date < self._today(),
                "version_id": version_row["version_id"],
            })
        return {"agreement_id": agreement_id, "version_id": version_row["version_id"], "obligations": result}

    def unmet_obligations(self, actor_id: str, candidate_id: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        sql = (
            "SELECT a.agreement_id,a.candidate_id,s.obligation_id,s.state,s.version_id "
            "FROM obligation_status s JOIN agreements a ON a.agreement_id=s.agreement_id "
            "WHERE s.state='open'"
        )
        args: list[Any] = []
        if candidate_id:
            sql += " AND a.candidate_id=?"
            args.append(candidate_id)
        rows = self.connection.execute(sql + " ORDER BY a.agreement_id,s.obligation_id", args).fetchall()
        items = []
        today_value = self._today()
        for row in rows:
            version_row = self._signed_version_row(row["agreement_id"])
            if version_row is None:
                continue
            content = self._load_content(version_row)
            obligation = next((o for o in content.obligations if o.obligation_id == row["obligation_id"]), None)
            if obligation is None or not obligation.required:
                continue
            items.append({
                "agreement_id": row["agreement_id"],
                "candidate_id": row["candidate_id"],
                "obligation_id": obligation.obligation_id,
                "kind": obligation.kind,
                "description": obligation.description,
                "assignee_party_id": obligation.assignee_party_id,
                "due_date": obligation.due_date,
                "overdue": obligation.due_date < today_value,
                "version_id": version_row["version_id"],
            })
        return {"as_of": today_value, "items": items}

    # ------------------------------------------------------------ 假设与预测

    def create_assumptions(self, actor_id: str, assumptions_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "assumptions.write")
        try:
            assumptions = AssumptionSet.from_dict(assumptions_id, raw)
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc
        content = assumptions.content_dict()
        sha = assumptions.content_sha256()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO assumptions(assumptions_id,currency,horizon_end,content_json,"
                    "content_sha256,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (assumptions_id, content["currency"], content["horizon_end"],
                     canonical_json(content), sha, actor_id, self._now()),
                )
                self._audit("assumptions", assumptions_id, "assumptions.created",
                            actor_id, {"content_sha256": sha})
        except sqlite3.IntegrityError as exc:
            raise Conflict("假设编号或内容已经存在") from exc
        return {"assumptions_id": assumptions_id, "content_sha256": sha, **content}

    def forecast(self, actor_id: str, assumptions_id: str, as_of_date: str) -> dict[str, Any]:
        self._require(actor_id, "forecast.run")
        assumptions_row = self.connection.execute(
            "SELECT * FROM assumptions WHERE assumptions_id=?", (assumptions_id,)
        ).fetchone()
        if assumptions_row is None:
            raise NotFound("假设集不存在")
        assumptions = AssumptionSet.from_dict(
            assumptions_id, json.loads(assumptions_row["content_json"])
        )
        # 每个协议取 as_of_date 当日“当时有效”的版本；窗口截断到版本到期日。
        agreements = self.connection.execute(
            "SELECT agreement_id FROM agreements WHERE state='active' ORDER BY agreement_id"
        ).fetchall()
        snapshot: list[dict[str, Any]] = []
        inflows: list[dict[str, Any]] = []
        totals: dict[str, dict[str, str]] = {}
        for agreement in agreements:
            version_row = self._effective_version_row(agreement["agreement_id"], as_of_date)
            if version_row is None:
                continue
            content = self._load_content(version_row)
            if content.currency != assumptions.currency:
                continue
            projected = project_inflows(
                agreement["agreement_id"], version_row["version_id"], content,
                assumptions, as_of_date,
                window_end=content.term_end_date,
            )
            snapshot.append({
                "agreement_id": agreement["agreement_id"],
                "version_id": version_row["version_id"],
                "content_sha256": version_row["content_sha256"],
                "effective_date": version_row["effective_date"],
                "term_end_date": version_row["term_end_date"],
            })
            for item in projected:
                inflows.append({
                    "agreement_id": item.agreement_id,
                    "version_id": item.version_id,
                    "term_id": item.term_id,
                    "kind": item.kind,
                    "period": item.period,
                    "region": item.region,
                    "indication": item.indication,
                    "gross": money_text(item.gross),
                    "currency": item.currency,
                    "basis": item.basis,
                })
                bucket = totals.setdefault(item.currency, {"gross": "0"})
                bucket["gross"] = money_text(Decimal(bucket["gross"]) + item.gross)
        basis = {"as_of_date": as_of_date, "assumptions_sha256": assumptions_row["content_sha256"],
                 "versions": snapshot}
        input_sha = hashlib.sha256(canonical_json(basis).encode("utf-8")).hexdigest()
        existing = self.connection.execute(
            "SELECT forecast_id,result_json FROM forecasts WHERE input_sha256=?", (input_sha,)
        ).fetchone()
        if existing is not None:
            return {"forecast_id": existing["forecast_id"], **json.loads(existing["result_json"]), "replayed": True}
        result = {
            "as_of_date": as_of_date,
            "assumptions_id": assumptions_id,
            "currency": assumptions.currency,
            "inflows": inflows,
            "totals": totals,
            "basis_versions": snapshot,
        }
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO forecasts(as_of_date,assumptions_id,assumptions_sha256,basis_json,"
                "input_sha256,result_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (as_of_date, assumptions_id, assumptions_row["content_sha256"],
                 canonical_json(basis), input_sha, canonical_json(result), actor_id, self._now()),
            )
            forecast_id = int(cursor.lastrowid)
            self._audit("forecast", str(forecast_id), "forecast.created", actor_id,
                        {"input_sha256": input_sha})
        return {"forecast_id": forecast_id, **result, "replayed": False}

    # ------------------------------------------------------------ 真实事件与逐期确认

    def record_event(self, actor_id: str, raw: Mapping[str, Any], idempotency_key: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "event.write")
        agreement_id = str(raw["agreement_id"]).strip()
        kind = str(raw["kind"]).strip()
        if kind not in {"milestone", "royalty"}:
            raise ValidationFailed("kind 必须是 milestone 或 royalty（首付款在签署版本时即已确立，不逐期确认）")
        event_id = str(raw["event_id"]).strip()
        term_id = str(raw["term_id"]).strip()
        period = str(raw["period"]).strip()
        event_date = str(raw["event_date"]).strip()
        face = Decimal(str(raw["face_amount"]))
        if face < 0:
            raise ValidationFailed("face_amount 不能为负")
        currency = str(raw.get("currency", "USD")).strip().upper()
        idem = idempotency_key or str(raw.get("idempotency_key") or event_id)
        with transaction(self.connection, immediate=True):
            stored = self.connection.execute(
                "SELECT event_id FROM revenue_events WHERE idempotency_key=?", (idem,)
            ).fetchone()
            if stored is not None:
                result = self.event(actor_id, stored["event_id"])
                result["duplicate"] = True
                return result
            agreement = self.connection.execute(
                "SELECT * FROM agreements WHERE agreement_id=?", (agreement_id,)
            ).fetchone()
            if agreement is None:
                raise NotFound("协议不存在")
            if agreement["state"] != "active":
                raise InvalidState("协议不在生效中，不能登记收益事件")
            # 关键：引用事件发生当日“当时有效”的版本，而非最新版本。
            version_row = self._effective_version_row(agreement_id, event_date)
            if version_row is None:
                raise InvalidState(f"{event_date} 没有生效中的条款版本，事件无权利来源")
            content = self._load_content(version_row)
            term = content.term(term_id)
            if term is None or term.kind != kind:
                raise ValidationFailed(f"条款 {term_id} 在版本 {version_row['version_id']} 中不存在或类型不符")
            if term.currency != currency:
                raise ValidationFailed("事件币种与条款币种不一致")
            region = raw.get("region")
            indication = raw.get("indication")
            if kind == "royalty":
                if not region or not indication:
                    raise ValidationFailed("royalty 事件必须提供 region 与 indication")
                region = str(region).strip().upper()
                indication = str(indication).strip().upper()
                if not content.covers_sale(region, indication):
                    raise InvalidState("销售地区/适应症不在该版本授权范围内")
            else:
                region = str(region).strip().upper() if region else None
                indication = str(indication).strip().upper() if indication else None
            source_note = str(raw.get("source_note", "")).strip() or "真实事件登记"
            self.connection.execute(
                "INSERT INTO revenue_events(event_id,agreement_id,version_id,term_id,kind,period,"
                "region,indication,face_amount,currency,event_date,source_note,idempotency_key,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (event_id, agreement_id, version_row["version_id"], term_id, kind, period,
                 region, indication, money_text(face), currency, event_date, source_note,
                 idem, actor_id, self._now()),
            )
            amounts = split_amount(face, term.shares)
            distribution_ids = []
            for share, amount in zip(term.shares, amounts):
                distribution_id = f"dist-{uuid.uuid4().hex[:16]}"
                distribution_ids.append(distribution_id)
                self.connection.execute(
                    "INSERT INTO distributions(distribution_id,event_id,agreement_id,version_id,"
                    "term_id,period,recipient_party_id,share_bp,amount,currency,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (distribution_id, event_id, agreement_id, version_row["version_id"], term_id,
                     period, share.recipient_party_id, share.share_bp, money_text(amount),
                     currency, self._now()),
                )
            self._audit("event", event_id, "event.recorded", actor_id, {
                "agreement_id": agreement_id, "version_id": version_row["version_id"],
                "term_id": term_id, "face_amount": money_text(face),
                "distribution_ids": distribution_ids,
            })
        return self.event(actor_id, event_id)

    def event(self, actor_id: str, event_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        row = self.connection.execute(
            "SELECT * FROM revenue_events WHERE event_id=?", (event_id,)
        ).fetchone()
        if row is None:
            raise NotFound("收益事件不存在")
        distributions = self.connection.execute(
            "SELECT * FROM distributions WHERE event_id=? ORDER BY recipient_party_id", (event_id,)
        ).fetchall()
        result = dict(row)
        result["distributions"] = [dict(d) for d in distributions]
        return result

    def confirm_distribution(self, actor_id: str, distribution_id: str) -> dict[str, Any]:
        self._require(actor_id, "distribution.confirm")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM distributions WHERE distribution_id=?", (distribution_id,)
            ).fetchone()
            if row is None:
                raise NotFound("分配不存在")
            if row["state"] == "frozen":
                raise InvalidState("分配处于争议冻结中，不能确认")
            if row["state"] != "pending":
                raise InvalidState(f"分配状态为 {row['state']}，不能确认")
            self.connection.execute(
                "UPDATE distributions SET state='confirmed',confirmed_at=? WHERE distribution_id=?",
                (self._now(), distribution_id),
            )
            self._audit("distribution", distribution_id, "distribution.confirmed", actor_id,
                        {"event_id": row["event_id"], "amount": row["amount"]})
        return self.distribution(actor_id, distribution_id)

    def settle_distribution(self, actor_id: str, distribution_id: str, note: str) -> dict[str, Any]:
        self._require(actor_id, "settlement.write")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM distributions WHERE distribution_id=?", (distribution_id,)
            ).fetchone()
            if row is None:
                raise NotFound("分配不存在")
            if row["state"] == "frozen":
                raise InvalidState("分配处于争议冻结中，不能结算")
            existing = self.connection.execute(
                "SELECT 1 FROM settlements WHERE distribution_id=?", (distribution_id,)
            ).fetchone()
            if existing is not None:
                raise Conflict("该分配已有结算流水；已结算金额不可重复结算或倒改")
            if row["state"] != "confirmed":
                raise InvalidState("只有已确认的分配可以结算")
            settled_at = self._now()
            self.connection.execute(
                "UPDATE distributions SET state='confirmed',confirmed_at=COALESCE(confirmed_at,?) "
                "WHERE distribution_id=?",
                (settled_at, distribution_id),
            )
            self.connection.execute(
                "INSERT INTO settlements(distribution_id,amount,currency,period,note,created_by,settled_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (distribution_id, row["amount"], row["currency"], row["period"], note,
                 actor_id, settled_at),
            )
            self._audit("distribution", distribution_id, "distribution.settled", actor_id,
                        {"amount": row["amount"], "currency": row["currency"]})
        return self.distribution(actor_id, distribution_id)

    def reverse_distribution(self, actor_id: str, distribution_id: str, note: str) -> dict[str, Any]:
        self._require(actor_id, "distribution.reverse")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM distributions WHERE distribution_id=?", (distribution_id,)
            ).fetchone()
            if row is None:
                raise NotFound("分配不存在")
            if self.connection.execute(
                "SELECT 1 FROM settlements WHERE distribution_id=?", (distribution_id,)
            ).fetchone():
                raise InvalidState("该分配已有结算流水，已结算金额不得冲正或被新版本倒改")
            if row["state"] == "frozen":
                raise InvalidState("争议冻结中的分配应先解决争议，不能直接冲正")
            if row["state"] != "pending":
                raise InvalidState(f"分配状态为 {row['state']}，不能冲正")
            self.connection.execute(
                "UPDATE distributions SET state='reversed',reversed_at=? WHERE distribution_id=?",
                (self._now(), distribution_id),
            )
            self._audit("distribution", distribution_id, "distribution.reversed",
                        actor_id, {"note": note})
        return self.distribution(actor_id, distribution_id)

    # ------------------------------------------------------------ 争议：只冻结相关份额

    def open_dispute(
        self, actor_id: str, event_id: str, reason: str, recipient_party_ids: Sequence[str]
    ) -> dict[str, Any]:
        self._require(actor_id, "dispute.write")
        if not recipient_party_ids:
            raise ValidationFailed("必须指定争议涉及的收款方；争议只冻结相关份额")
        if not reason.strip():
            raise ValidationFailed("争议理由不能为空")
        dispute_id = f"disp-{uuid.uuid4().hex[:16]}"
        with transaction(self.connection, immediate=True):
            event_row = self.connection.execute(
                "SELECT * FROM revenue_events WHERE event_id=?", (event_id,)
            ).fetchone()
            if event_row is None:
                raise NotFound("收益事件不存在")
            existing_open = self.connection.execute(
                "SELECT 1 FROM disputes WHERE event_id=? AND state='open'", (event_id,)
            ).fetchone()
            if existing_open is not None:
                raise Conflict("该事件已有未解决争议")
            wanted = {p.strip() for p in recipient_party_ids}
            distributions = self.connection.execute(
                "SELECT * FROM distributions WHERE event_id=? ORDER BY recipient_party_id", (event_id,)
            ).fetchall()
            present = {d["recipient_party_id"] for d in distributions}
            unknown = wanted - present
            if unknown:
                raise ValidationFailed(f"收款方不在该事件的分成名单中：{sorted(unknown)}")
            frozen_parties: list[str] = []
            skipped_settled: list[str] = []
            for distribution in distributions:
                if distribution["recipient_party_id"] not in wanted:
                    continue
                is_settled = self.connection.execute(
                    "SELECT 1 FROM settlements WHERE distribution_id=?",
                    (distribution["distribution_id"],),
                ).fetchone()
                if is_settled:
                    skipped_settled.append(distribution["recipient_party_id"])
                    continue
                if distribution["state"] == "reversed":
                    continue
                frozen_parties.append(distribution["recipient_party_id"])
            if not frozen_parties:
                raise InvalidState("相关份额均已结算或冲正，没有可冻结的金额；争议不影响已结算金额")
            self.connection.execute(
                "INSERT INTO disputes(dispute_id,event_id,reason,frozen_recipients_json,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (dispute_id, event_id, reason, canonical_json(sorted(frozen_parties)),
                 actor_id, self._now()),
            )
            for party_id in frozen_parties:
                self.connection.execute(
                    "UPDATE distributions SET state='frozen',dispute_id=? "
                    "WHERE event_id=? AND recipient_party_id=?",
                    (dispute_id, event_id, party_id),
                )
            self.connection.execute(
                "UPDATE revenue_events SET dispute_id=?,"
                "state=CASE WHEN (SELECT count(*) FROM distributions WHERE event_id=? AND state NOT IN "
                "('frozen','reversed'))=0 THEN 'frozen_full' ELSE 'frozen_partial' END WHERE event_id=?",
                (dispute_id, event_id, event_id),
            )
            self._audit("dispute", dispute_id, "dispute.opened", actor_id, {
                "event_id": event_id, "frozen": sorted(frozen_parties),
                "skipped_settled": skipped_settled,
            })
        return self.dispute(actor_id, dispute_id)

    def resolve_dispute(self, actor_id: str, dispute_id: str, resolution_note: str) -> dict[str, Any]:
        self._require(actor_id, "dispute.write")
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT * FROM disputes WHERE dispute_id=?", (dispute_id,)
            ).fetchone()
            if row is None:
                raise NotFound("争议不存在")
            if row["state"] != "open":
                raise InvalidState("争议已解决")
            self.connection.execute(
                "UPDATE distributions SET state='pending',dispute_id=NULL WHERE dispute_id=? AND state='frozen'",
                (dispute_id,),
            )
            self.connection.execute(
                "UPDATE revenue_events SET state='recorded' WHERE dispute_id=? AND state IN "
                "('frozen_partial','frozen_full')",
                (dispute_id,),
            )
            self.connection.execute(
                "UPDATE disputes SET state='resolved',resolved_at=?,resolution_note=? WHERE dispute_id=?",
                (self._now(), resolution_note, dispute_id),
            )
            self._audit("dispute", dispute_id, "dispute.resolved", actor_id, {"note": resolution_note})
        return self.dispute(actor_id, dispute_id)

    def dispute(self, actor_id: str, dispute_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        row = self.connection.execute("SELECT * FROM disputes WHERE dispute_id=?", (dispute_id,)).fetchone()
        if row is None:
            raise NotFound("争议不存在")
        result = dict(row)
        result["frozen_recipients"] = json.loads(row["frozen_recipients_json"])
        return result

    def distribution(self, actor_id: str, distribution_id: str) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        row = self.connection.execute(
            "SELECT * FROM distributions WHERE distribution_id=?", (distribution_id,)
        ).fetchone()
        if row is None:
            raise NotFound("分配不存在")
        result = dict(row)
        result["settlements"] = [dict(s) for s in self.connection.execute(
            "SELECT * FROM settlements WHERE distribution_id=? ORDER BY settlement_id",
            (distribution_id,),
        ).fetchall()]
        return result

    def distribution_basis(self, actor_id: str, distribution_id: str) -> dict[str, Any]:
        """任一笔分配的完整依据：事件、当时有效版本中的分成条款、结算流水。"""
        self._require(actor_id, "report.read")
        row = self.connection.execute(
            "SELECT * FROM distributions WHERE distribution_id=?", (distribution_id,)
        ).fetchone()
        if row is None:
            raise NotFound("分配不存在")
        version_row = self._version_row(row["version_id"])
        content = self._load_content(version_row)
        term = content.term(row["term_id"])
        event_row = self.connection.execute(
            "SELECT * FROM revenue_events WHERE event_id=?", (row["event_id"],)
        ).fetchone()
        settlements = [dict(s) for s in self.connection.execute(
            "SELECT * FROM settlements WHERE distribution_id=? ORDER BY settlement_id",
            (distribution_id,),
        ).fetchall()]
        return {
            "distribution": dict(row),
            "event": dict(event_row),
            "rights_source": {
                "version_id": row["version_id"],
                "agreement_id": row["agreement_id"],
                "content_sha256": version_row["content_sha256"],
                "effective_date": version_row["effective_date"],
                "term_end_date": version_row["term_end_date"],
                "term": term.to_dict() if term else None,
            },
            "settlements": settlements,
        }

    # ------------------------------------------------------------ 追溯：授权链与台账

    def authorization_chain(self, actor_id: str, agreement_id: str) -> dict[str, Any]:
        self._require(actor_id, "chain.read")
        root = self.connection.execute(
            "SELECT * FROM agreements WHERE agreement_id=?", (agreement_id,)
        ).fetchone()
        if root is None:
            raise NotFound("协议不存在")

        def walk_up(start: str | None) -> list[str]:
            chain: list[str] = []
            seen: set[str] = set()
            current = start
            while current is not None and current not in seen:
                seen.add(current)
                chain.append(current)
                row = self.connection.execute(
                    "SELECT parent_agreement_id FROM agreements WHERE agreement_id=?", (current,)
                ).fetchone()
                current = row["parent_agreement_id"] if row else None
            return chain

        def agreement_view(row: sqlite3.Row) -> dict[str, Any]:
            versions = [
                self._version_view(v) for v in self.connection.execute(
                    "SELECT * FROM agreement_versions WHERE agreement_id=? ORDER BY seq", (row["agreement_id"],)
                ).fetchall()
            ]
            events = [dict(e) for e in self.connection.execute(
                "SELECT event_id,version_id,term_id,kind,period,face_amount,currency,state "
                "FROM revenue_events WHERE agreement_id=? ORDER BY event_date,event_id",
                (row["agreement_id"],),
            ).fetchall()]
            return {
                "agreement_id": row["agreement_id"],
                "agreement_no": row["agreement_no"],
                "candidate_id": row["candidate_id"],
                "direction": row["direction"],
                "state": row["state"],
                "parent_agreement_id": row["parent_agreement_id"],
                "renewal_of_agreement_id": row["renewal_of_agreement_id"],
                "upstream_chain": walk_up(row["parent_agreement_id"]),
                "versions": versions,
                "events": events,
            }

        chain_ids = [agreement_id] + walk_up(root["parent_agreement_id"])
        renewal_ids: list[str] = []
        seen_renewal: set[str] = set()
        current = root["renewal_of_agreement_id"]
        while current is not None and current not in seen_renewal:
            seen_renewal.add(current)
            renewal_ids.append(current)
            row = self.connection.execute(
                "SELECT renewal_of_agreement_id FROM agreements WHERE agreement_id=?", (current,)
            ).fetchone()
            current = row["renewal_of_agreement_id"] if row else None
        nodes = {
            aid: agreement_view(self.connection.execute(
                "SELECT * FROM agreements WHERE agreement_id=?", (aid,)
            ).fetchone())
            for aid in chain_ids + renewal_ids
        }
        unmet = self.unmet_obligations(actor_id, root["candidate_id"])
        return {
            "root_agreement_id": agreement_id,
            "candidate_id": root["candidate_id"],
            "rights_chain_order": chain_ids,
            "renewal_chain_order": [agreement_id] + renewal_ids,
            "agreements": nodes,
            "candidate_unmet_obligations": unmet["items"],
        }

    def trace_region(self, actor_id: str, region: str) -> dict[str, Any]:
        """从任一地区反查覆盖该地区的生效授权及其权利链。"""
        self._require(actor_id, "chain.read")
        token = region.strip().upper()
        matches = []
        rows = self.connection.execute(
            "SELECT v.* FROM agreement_versions v JOIN agreements a ON a.agreement_id=v.agreement_id "
            "WHERE v.state='effective' AND a.state='active' ORDER BY v.agreement_id,v.seq"
        ).fetchall()
        for row in rows:
            content = self._load_content(row)
            covering = [s.to_dict() for s in content.scopes if s.covers_region(token)]
            if covering:
                agreement = self.connection.execute(
                    "SELECT candidate_id,direction,parent_agreement_id FROM agreements WHERE agreement_id=?",
                    (row["agreement_id"],),
                ).fetchone()
                matches.append({
                    "agreement_id": row["agreement_id"],
                    "candidate_id": agreement["candidate_id"],
                    "direction": agreement["direction"],
                    "version_id": row["version_id"],
                    "effective_date": row["effective_date"],
                    "term_end_date": row["term_end_date"],
                    "scopes": covering,
                })
        return {"region": token, "as_of": self._today(), "grants": matches}

    def trace_candidate(self, actor_id: str, candidate_id: str) -> dict[str, Any]:
        self._require(actor_id, "chain.read")
        agreements = self.connection.execute(
            "SELECT agreement_id FROM agreements WHERE candidate_id=? ORDER BY agreement_id",
            (candidate_id,),
        ).fetchall()
        return {
            "candidate_id": candidate_id,
            "agreement_ids": [a["agreement_id"] for a in agreements],
            "chains": [self.authorization_chain(actor_id, a["agreement_id"]) for a in agreements],
            "unmet_obligations": self.unmet_obligations(actor_id, candidate_id)["items"],
        }

    def ledger_report(self, actor_id: str, *, period: str | None = None,
                      recipient_party_id: str | None = None) -> dict[str, Any]:
        self._require(actor_id, "report.read")
        sql = "SELECT * FROM distributions WHERE 1=1"
        args: list[Any] = []
        if period:
            sql += " AND period=?"
            args.append(period)
        if recipient_party_id:
            sql += " AND recipient_party_id=?"
            args.append(recipient_party_id)
        sql += " ORDER BY period,distribution_id"
        totals: dict[str, dict[str, str]] = {}
        items = []
        for row in self.connection.execute(sql, args).fetchall():
            item = dict(row)
            item["settled"] = self.connection.execute(
                "SELECT count(*) FROM settlements WHERE distribution_id=?", (row["distribution_id"],)
            ).fetchone()[0] > 0
            items.append(item)
            bucket = totals.setdefault(row["currency"], {
                "pending": "0", "confirmed": "0", "frozen": "0",
                "settled": "0", "reversed": "0",
            })
            key = "settled" if item["settled"] else row["state"]
            if key in bucket:
                bucket[key] = money_text(Decimal(bucket[key]) + Decimal(row["amount"]))
        return {"as_of": self._today(), "period": period, "recipient_party_id": recipient_party_id,
                "distributions": items, "totals": totals}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM lic_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
