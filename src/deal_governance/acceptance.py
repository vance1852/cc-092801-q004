"""离线命令行验收：版本化授权链、签署前检查、收益回流与争议冻结。"""

from __future__ import annotations

import argparse
import json
from datetime import date

from .clock import FrozenClock
from .errors import InvalidState, SigningBlocked
from .service import DealGovernanceService
from .storage import connect


def _terms(*, licensor, licensee, territories, stages, exclusivity="exclusive",
           sublicense_scope="same-scope", share="0.25", royalty="0.12",
           upfront="10000000", expiry="2030-12-31", source=None, obligations=None,
           milestones=None, effective="2026-02-01", note="初始版本"):
    return {
        "effective_date": effective,
        "expiry_date": expiry,
        "licensor_party_id": licensor,
        "licensee_party_id": licensee,
        "scope": {"territories": territories, "indications": ["ONC"], "stages": stages},
        "exclusivity": exclusivity,
        "sublicense_scope": sublicense_scope,
        "sublicense_income_share": share,
        "upfront_amount": upfront,
        "milestones": milestones or [],
        "royalty": {"rate": royalty} if royalty else None,
        "obligations": obligations if obligations is not None else [
            {"obligation_id": "dev1", "kind": "development",
             "description": "2027 年底前完成 III 期首例入组", "due_date": "2027-12-31",
             "due_event": None, "owner_party_id": licensee},
            {"obligation_id": "com1", "kind": "commercial",
             "description": "获批后 12 个月内上市", "due_date": None,
             "due_event": "approval+12m", "owner_party_id": licensee},
        ],
        "source_revision_id": source,
        "change_note": note,
    }


def run(database: str = ":memory:") -> dict:
    clock = FrozenClock(date(2026, 2, 1))
    service = DealGovernanceService(connect(database), clock)

    for uid, name, role in (("bd", "商务拓展", "bd"), ("manager", "授权管理", "manager"),
                            ("finance", "财务确认", "finance"), ("auditor", "审计追溯", "auditor")):
        service.create_user(None, uid, name, role)

    service.register_candidate("bd", {"candidate_id": "C-1", "name": "抗肿瘤候选药 X-01"})
    for pid, name in (("P_BIO", "本公司"), ("P_EU", "欧洲合作方"),
                      ("P_JP", "日本合作方"), ("P_KR", "韩国合作方")):
        service.register_party("bd", {"party_id": pid, "name": name})

    # 1) 源头授权：本公司 -> 欧洲合作方，覆盖 EU+JP
    service.create_agreement("bd", "A-EU", "C-1", "X-01 海外授权协议")
    service.draft_revision("bd", "A-EU", _terms(
        licensor="P_BIO", licensee="P_EU", territories=["EU", "JP"],
        stages=["phase2", "phase3", "approved"],
        milestones=[{"milestone_id": "m1", "milestone_type": "development",
                     "name": "III 期启动", "amount": "20000000",
                     "trigger_event": "phase3 first patient in"}],
        note="首轮谈判版本"))
    findings_eu = service.pre_sign_findings("manager", "A-EU-r1")
    service.sign_revision("manager", "A-EU-r1")

    # 2) 排他冲突：试图就同一地区 JP 向第三方再签排他协议 -> blocker
    service.create_agreement("bd", "A-KR", "C-1", "韩国方日本权益洽谈")
    service.draft_revision("bd", "A-KR", _terms(
        licensor="P_BIO", licensee="P_KR", territories=["JP"],
        stages=["approved"], obligations=[
            {"obligation_id": "c", "kind": "commercial", "description": "上市",
             "due_date": None, "due_event": "approval+12m", "owner_party_id": "P_KR"}],
        sublicense_scope="none", share="0", note="竞标草案"))
    blocked = service.pre_sign_findings("manager", "A-KR-r1")
    blocked_codes = {f["code"] for f in blocked}
    try:
        service.sign_revision("manager", "A-KR-r1")
        raise AssertionError("排他冲突版本不应签署成功")
    except SigningBlocked as exc:
        signing_blocked = [f["code"] for f in exc.findings]
    service.terminate_revision("manager", "A-KR-r1", "排他冲突，终止洽谈草案")

    # 3) 义务缺口：覆盖临床阶段却无开发义务 -> blocker（KR 与既有授权不重叠）
    service.create_agreement("bd", "A-GAP", "C-1", "义务缺口示例")
    service.draft_revision("bd", "A-GAP", _terms(
        licensor="P_BIO", licensee="P_KR", territories=["KR"],
        stages=["phase3"], sublicense_scope="none", share="0",
        obligations=[{"obligation_id": "r", "kind": "reporting",
                      "description": "年报", "due_date": "2027-01-01",
                      "due_event": None, "owner_party_id": "P_KR"}],
        note="缺开发义务"))
    gap_findings = service.pre_sign_findings("manager", "A-GAP-r1")
    gap_codes = [f["code"] for f in gap_findings if f["severity"] == "blocker"]
    service.terminate_revision("manager", "A-GAP-r1", "义务缺口，退回谈判")

    # 4) 再许可：欧洲方 -> 日本方，引用当时有效的 A-EU-r1，范围不越权
    service.create_agreement("bd", "A-JP", "C-1", "日本再许可协议")
    service.draft_revision("bd", "A-JP", _terms(
        licensor="P_EU", licensee="P_JP", territories=["JP"],
        stages=["approved"], exclusivity="non-exclusive",
        sublicense_scope="none", share="0", royalty="0.08", upfront="5000000",
        source="A-EU-r1",
        obligations=[{"obligation_id": "jp1", "kind": "commercial",
                      "description": "日本获批后 6 个月内上市", "due_date": None,
                      "due_event": "jp_approval+6m", "owner_party_id": "P_JP"}],
        note="基于 A-EU-r1 的再许可"))
    jp_findings = service.pre_sign_findings("manager", "A-JP-r1")
    service.sign_revision("manager", "A-JP-r1")

    # 5) 越权再许可被拒绝：JP 方无权再许可，且范围超出上游
    service.create_agreement("bd", "A-BAD", "C-1", "越权再许可")
    try:
        service.draft_revision("bd", "A-BAD", _terms(
            licensor="P_JP", licensee="P_KR", territories=["EU"],
            stages=["approved"], exclusivity="exclusive",
            sublicense_scope="none", share="0", source="A-JP-r1",
            note="越权"))
        raise AssertionError("越权再许可不应登记成功")
    except Exception as exc:
        overreach_reason = str(exc)

    # 6) 可复算预测：同快照+同假设，两次预测 lines_hash 相同
    assumptions = {
        "milestone_probabilities": {"m1": "0.75"},
        "annual_net_sales": {"2029": "100000000", "2030": "200000000"},
    }
    p1 = service.create_projection("finance", "A-EU-r1", assumptions)
    p2 = service.create_projection("finance", "A-EU-r1", assumptions, commit=True)
    assert p1["lines_hash"] == p2["lines_hash"]

    # 7) 真实事件逐期确认：销售分成沿授权链回流
    royalty_event = service.record_payment_event("finance", {
        "revision_id": "A-EU-r1", "payment_kind": "royalty",
        "event_date": "2029-06-30", "period_label": "2029H1",
        "basis_amount": "100000000", "currency": "USD",
        "idempotency_key": "roy-2029h1"})
    direct_id = next(a["allocation_id"] for a in royalty_event["allocations"])
    service.settle_allocation("finance", direct_id, "WIRE-2029-001")

    # 再许可首付款：75% 欧洲方留存，25% 回流本公司
    sub_upfront = service.record_payment_event("finance", {
        "revision_id": "A-JP-r1", "payment_kind": "sublicense",
        "event_date": "2026-03-01", "period_label": "2026-03",
        "gross_amount": "5000000", "currency": "USD",
        "idempotency_key": "sub-upfront-1"})
    passthrough_upfront = next(a for a in sub_upfront["allocations"] if a["flow_role"] == "passthrough")
    service.settle_allocation("finance", passthrough_upfront["allocation_id"], "WIRE-2026-002")

    # 再许可销售分成：日本方按 8% 付给欧洲方，上游本公司按 12% 同步计提
    sub_royalty = service.record_payment_event("finance", {
        "revision_id": "A-JP-r1", "payment_kind": "royalty",
        "event_date": "2029-09-30", "period_label": "2029Q3",
        "basis_amount": "50000000", "currency": "USD",
        "idempotency_key": "sub-roy-2029q3"})
    bio_passthrough = next(a for a in sub_royalty["allocations"]
                           if a["recipient_party_id"] == "P_BIO")

    # 8) 争议只冻结相关份额：6M 上游分成 held，其余份额照常结算
    dispute = service.open_dispute("finance", sub_royalty["event_id"],
                                   [bio_passthrough["allocation_id"]],
                                   "日本净销售额口径争议")
    eu_direct = next(a for a in sub_royalty["allocations"] if a["flow_role"] == "direct")
    service.settle_allocation("finance", eu_direct["allocation_id"], "WIRE-2029-003")
    try:
        service.settle_allocation("finance", bio_passthrough["allocation_id"], "X")
        raise AssertionError("冻结份额不能结算")
    except InvalidState:
        pass
    # 已结算份额不能被争议倒改
    try:
        service.open_dispute("finance", royalty_event["event_id"], [direct_id], "试图冻结已结算")
        raise AssertionError("已结算份额不能冻结")
    except InvalidState:
        pass
    service.resolve_dispute("finance", dispute["dispute_id"], "release", note="口径核对无误，解冻")
    resolved_event = service.get_event("finance", sub_royalty["event_id"])
    released_state = next(a["state"] for a in resolved_event["allocations"]
                          if a["allocation_id"] == bio_passthrough["allocation_id"])

    # 9) 新版本不倒改历史：A-EU-r2 调整分成比例，r1 已结算金额不变
    service.draft_revision("bd", "A-EU", _terms(
        licensor="P_BIO", licensee="P_EU", territories=["EU", "JP"],
        stages=["phase3", "approved"], royalty="0.14",
        milestones=[], note="分成比例修订 12% -> 14%",
        effective="2030-01-01"))
    service.sign_revision("manager", "A-EU-r2")
    settled_after_amendment = service.allocation_basis("auditor", candidate_id="C-1")
    old_settled = [a for a in settled_after_amendment
                   if a["allocation_id"] == direct_id][0]
    assert old_settled["amount"] == "12000000.00" and old_settled["state"] == "settled"
    assert p1["lines_hash"] == service.get_projection("auditor", p1["projection_id"])["lines_hash"]

    # 10) 续约不自动延长旧权利：2031 年 r2 已到期，必须显式签新约
    clock.advance(days=1826)  # 2031-02-01
    try:
        service.record_payment_event("finance", {
            "revision_id": "A-EU-r2", "payment_kind": "royalty",
            "event_date": "2031-02-01", "period_label": "2031H1",
            "basis_amount": "10000000", "currency": "USD",
            "idempotency_key": "roy-2031h1"})
        raise AssertionError("到期版本不应继续确认收益")
    except InvalidState as exc:
        expiry_reason = str(exc)
    service.create_agreement("bd", "A-RENEW", "C-1", "X-01 续约协议")
    service.draft_revision("bd", "A-RENEW", _terms(
        licensor="P_BIO", licensee="P_EU", territories=["EU"],
        stages=["approved"], royalty="0.15", milestones=[],
        effective="2031-02-01", expiry="2036-12-31", note="显式续约"))
    service.sign_revision("manager", "A-RENEW-r1")
    renewed = service.record_payment_event("finance", {
        "revision_id": "A-RENEW-r1", "payment_kind": "royalty",
        "event_date": "2031-03-31", "period_label": "2031Q1",
        "basis_amount": "10000000", "currency": "USD",
        "idempotency_key": "roy-2031q1"})

    # 11) 追溯：授权链、未满足义务、每笔分配依据
    chain = service.rights_chain("auditor", "A-JP-r1")
    obligations = service.open_obligations("auditor", candidate_id="C-1")
    trace_jp = service.territory_trace("auditor", territory="JP")

    return {
        "status": "ok",
        "signed_revisions": ["A-EU-r1", "A-JP-r1", "A-EU-r2", "A-RENEW-r1"],
        "eu_pre_sign_findings": findings_eu,
        "blocked_signing_codes": signing_blocked,
        "blocked_codes_seen": sorted(blocked_codes),
        "obligation_gap": gap_codes,
        "jp_pre_sign_warnings": [f["code"] for f in jp_findings],
        "overreach_rejected": overreach_reason,
        "projection_lines_hash": p1["lines_hash"],
        "projection_expected_total": p2["expected_total"],
        "royalty_direct_amount": royalty_event["allocations"][0]["amount"],
        "sublicense_upfront": {a["flow_role"]: a["amount"] for a in sub_upfront["allocations"]},
        "sublicense_royalty": {a["flow_role"]: a["amount"] for a in sub_royalty["allocations"]},
        "dispute_states": {a["allocation_id"]: a["state"] for a in dispute["allocations"]},
        "post_resolution_state": released_state,
        "historical_settled_unchanged": old_settled["amount"],
        "expiry_enforced": expiry_reason,
        "renewed_event_amount": renewed["allocations"][0]["amount"],
        "rights_chain_levels": [c["revision_id"] for c in chain["chain"]],
        "open_obligation_count": len(obligations["obligations"]),
        "overdue_obligation_count": obligations["overdue_count"],
        "japan_trace_revisions": [r["revision_id"] for r in trace_jp],
        "allocation_basis_count": len(settled_after_amendment),
        "audit_events": len(service.audit_chain("auditor")),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="授权与收益治理离线验收")
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--database", default=":memory:")
    args = parser.parse_args()
    print(json.dumps(run(args.database), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
