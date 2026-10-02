"""离线命令行验收：版本化海外授权与收益台账完整业务闭环。

故事线：
1. 上游生物科技 UP 将候选药 X-101 的全球权益授权给我司 USCO（含共同开发义务与再许可限制）；
2. 我司向欧洲伙伴 EUCO 做下游授权，签署前评审核验权利来源、义务承接与再许可范围；
3. 与日方 JPCO 就重叠的欧洲排他范围谈判，评审阻断签署；
4. 基于假设形成可复算收益预测；
5. 登记真实销售事件，逐期确认分成；争议只冻结相关份额，其余份额照常结算；
6. 条款出 v2 修订；旧事件仍引用 v1，新版本不能倒改已结算金额；
7. 续约不得在旧权利到期前生效；从地区与候选药追溯授权链、未满足义务和每笔分配依据。
"""
from __future__ import annotations

import argparse
import json
import sqlite3

from .service_ledger import LicensingService
from .storage import connect


def _version(
    effective_date,
    term_end_date,
    *,
    regions,
    exclusive,
    sublicense,
    obligations,
    terms,
    indications=("ONC",),
    stages=("ANY",),
    therapy_field="ANY",
    excludes=(),
    currency="USD",
):
    return {
        "effective_date": effective_date,
        "term_end_date": term_end_date,
        "currency": currency,
        "scopes": [{
            "regions": list(regions),
            "region_excludes": list(excludes),
            "indications": list(indications),
            "stages": list(stages),
            "exclusive": exclusive,
            "therapy_field": therapy_field,
        }],
        "sublicense": sublicense,
        "obligations": obligations,
        "terms": terms,
    }


def run(database: str = ":memory:") -> dict:
    connection = connect(database)
    svc = LicensingService(connection)

    for uid, name, role in (
        ("bd", "商务经理", "bd_manager"),
        ("deal", "签约代表", "dealmaker"),
        ("fin", "财务", "finance"),
        ("aud", "审计", "auditor"),
    ):
        svc.create_user("", uid, name, role)

    svc.register_candidate("bd", "X-101", "抗肿瘤候选药", "X101")
    svc.register_party("bd", "UP", "上游生物科技", "licensor")
    svc.register_party("bd", "USCO", "我司", "licensee")
    svc.register_party("bd", "EUCO", "欧洲伙伴", "partner")
    svc.register_party("bd", "JPCO", "日本伙伴", "partner")

    upstream_obligations = [
        {"obligation_id": "cofund", "kind": "co_development", "description": "承担 III 期共同开发费用 30%",
         "assignee_party_id": "USCO", "due_date": "2028-12-31", "required": True},
        {"obligation_id": "report", "kind": "safety_report", "description": "每季度提交安全报告",
         "assignee_party_id": "USCO", "due_date": "2030-12-31", "required": True},
    ]
    upstream_terms = [
        {"term_id": "upfront", "kind": "upfront", "label": "首付款", "amount": "50000000",
         "due_date": "2026-11-01", "shares": [{"recipient_party_id": "UP", "share_bp": 10000}]},
        {"term_id": "ms_pha3", "kind": "milestone", "label": "III 期启动里程碑", "amount": "30000000",
         "trigger_stage": "phase3", "shares": [{"recipient_party_id": "UP", "share_bp": 10000}]},
        {"term_id": "roy", "kind": "royalty", "label": "分层销售分成",
         "tiers": [{"up_to": "200000000", "rate_percent": "10"},
                   {"up_to": None, "rate_percent": "14"}],
         "shares": [{"recipient_party_id": "UP", "share_bp": 10000}]},
    ]
    svc.create_agreement("bd", {
        "agreement_id": "IN-1", "agreement_no": "LIC-2026-001", "candidate_id": "X-101",
        "direction": "inbound", "licensor_party_id": "UP", "licensee_party_id": "USCO",
    })
    svc.draft_version("bd", "IN-1", _version(
        "2026-11-01", "2031-12-31", regions=("WORLD",), exclusive=True,
        sublicense={"scope": "named", "named_parties": ["EUCO"]},
        obligations=upstream_obligations, terms=upstream_terms,
    ))
    review_in = svc.run_review("bd", "IN-1:v1")
    signed_in = svc.sign_version("deal", "IN-1:v1")
    assert signed_in["state"] == "effective"

    # 下游欧洲授权：必须承接共同开发义务，且指名 EUCO 在上游名单内。
    downstream_obligations = [
        {"obligation_id": "cofund", "kind": "co_development", "description": "承担 III 期共同开发费用 30%",
         "assignee_party_id": "USCO", "due_date": "2028-12-31", "required": True},
        {"obligation_id": "report", "kind": "safety_report", "description": "每季度提交安全报告",
         "assignee_party_id": "USCO", "due_date": "2030-12-31", "required": True},
        {"obligation_id": "eu_dev", "kind": "co_development", "description": "与 EUCO 共同完成欧洲注册研究",
         "assignee_party_id": "EUCO", "due_date": "2029-06-30", "required": True},
    ]
    downstream_terms = [
        {"term_id": "eu_roy", "kind": "royalty", "label": "欧洲销售分成",
         "tiers": [{"up_to": "100000000", "rate_percent": "18"},
                   {"up_to": None, "rate_percent": "22"}],
         "shares": [{"recipient_party_id": "USCO", "share_bp": 8000},
                    {"recipient_party_id": "UP", "share_bp": 2000}]},
    ]
    svc.create_agreement("bd", {
        "agreement_id": "OUT-EU", "agreement_no": "LIC-2027-002", "candidate_id": "X-101",
        "direction": "outbound", "licensor_party_id": "USCO", "licensee_party_id": "EUCO",
        "parent_agreement_id": "IN-1",
    })
    svc.draft_version("bd", "OUT-EU", _version(
        "2027-01-01", "2031-06-30", regions=("EU", "UK"), exclusive=True,
        sublicense={"scope": "none"}, obligations=downstream_obligations, terms=downstream_terms,
    ))
    review_eu = svc.run_review("bd", "OUT-EU:v1")
    assert review_eu["blocking_count"] == 0, review_eu
    svc.sign_version("deal", "OUT-EU:v1")

    # 义务缺口场景：漏掉 cofund 的在谈版本必须被评审阻断。
    svc.create_agreement("bd", {
        "agreement_id": "OUT-EU-BAD", "agreement_no": "LIC-2027-009", "candidate_id": "X-101",
        "direction": "outbound", "licensor_party_id": "USCO", "licensee_party_id": "JPCO",
        "parent_agreement_id": "IN-1",
    })
    svc.draft_version("bd", "OUT-EU-BAD", _version(
        "2027-02-01", "2030-12-31", regions=("APAC",), exclusive=False,
        sublicense={"scope": "any"}, obligations=[], terms=downstream_terms,
    ))
    review_bad = svc.run_review("bd", "OUT-EU-BAD:v1")
    assert review_bad["blocking_count"] >= 2  # 再许可越权 + 义务缺口
    blocked_sign = None
    try:
        svc.sign_version("deal", "OUT-EU-BAD:v1")
    except Exception as exc:  # noqa: BLE001
        blocked_sign = type(exc).__name__
    svc.discard_version("deal", "OUT-EU-BAD:v1")

    # 排他冲突场景：JPCO 要求与欧洲授权时间窗重叠的 EU 排他范围。
    svc.create_agreement("bd", {
        "agreement_id": "OUT-JP", "agreement_no": "LIC-2027-003", "candidate_id": "X-101",
        "direction": "outbound", "licensor_party_id": "USCO", "licensee_party_id": "JPCO",
        "parent_agreement_id": "IN-1",
    })
    svc.draft_version("bd", "OUT-JP", _version(
        "2027-03-01", "2031-03-01", regions=("EU",), exclusive=True,
        sublicense={"scope": "none"}, obligations=downstream_obligations, terms=downstream_terms,
    ))
    review_jp = svc.run_review("bd", "OUT-JP:v1")
    assert any(i["code"] == "exclusivity_conflict" for i in review_jp["issues"])
    svc.discard_version("deal", "OUT-JP:v1")

    # 假设与可复算预测。
    svc.create_assumptions("fin", "ASM-1", {
        "currency": "USD", "horizon_end": "2031-12-31",
        "sales": [
            {"period": "2027", "region": "EU", "indication": "ONC", "net_sales": "150000000"},
            {"period": "2028", "region": "EU", "indication": "ONC", "net_sales": "300000000"},
            {"period": "2028", "region": "NA", "indication": "ONC", "net_sales": "250000000"},
        ],
        "milestones": [{"term_id": "ms_pha3", "probability": "0.7"}],
    })
    forecast_a = svc.forecast("fin", "ASM-1", "2027-01-01")
    forecast_b = svc.forecast("fin", "ASM-1", "2027-01-01")
    assert forecast_a["forecast_id"] == forecast_b["forecast_id"] and forecast_b["replayed"] is True

    # 真实事件：2027 年欧洲销售 1.5 亿 → 18% = 2700 万，按 80/20 拆分（尾差可复算）。
    event = svc.record_event("fin", {
        "event_id": "EV-2027-EU", "agreement_id": "OUT-EU", "kind": "royalty",
        "term_id": "eu_roy", "period": "2027", "region": "EU", "indication": "ONC",
        "face_amount": "27000000", "currency": "USD", "event_date": "2028-03-15",
        "source_note": "EUCO 2027 年度销售报告",
        "idempotency_key": "ev-2027-eu-onc",
    })
    assert event["version_id"] == "OUT-EU:v1"
    dist_up = next(d for d in event["distributions"] if d["recipient_party_id"] == "UP")
    dist_us = next(d for d in event["distributions"] if d["recipient_party_id"] == "USCO")

    # 争议只冻结 UP 的转付份额；USCO 份额不受影响，照常确认并结算。
    svc.open_dispute("fin", "EV-2027-EU", "UP 对转付口径提出异议", ["UP"])
    svc.confirm_distribution("fin", dist_us["distribution_id"])
    svc.settle_distribution("fin", dist_us["distribution_id"], "2027 年度分成结算")
    frozen_view = svc.event("fin", "EV-2027-EU")
    assert frozen_view["state"] == "frozen_partial"

    # 已结算份额不得冲正/冻结；争议期间 UP 份额不能确认。
    settle_protected = None
    try:
        svc.reverse_distribution("fin", dist_us["distribution_id"], "试图倒改已结算金额")
    except Exception as exc:  # noqa: BLE001
        settle_protected = type(exc).__name__
    confirm_blocked = None
    try:
        svc.confirm_distribution("fin", dist_up["distribution_id"])
    except Exception as exc:  # noqa: BLE001
        confirm_blocked = type(exc).__name__

    # 条款 v2 修订（费率调整）：签署后旧事件仍锁定 v1，新事件引用 v2。
    svc.draft_version("bd", "OUT-EU", _version(
        "2028-01-01", "2031-06-30", regions=("EU", "UK"), exclusive=True,
        sublicense={"scope": "none"},
        obligations=downstream_obligations,
        terms=[{
            "term_id": "eu_roy", "kind": "royalty", "label": "欧洲销售分成(v2 费率)",
            "tiers": [{"up_to": None, "rate_percent": "20"}],
            "shares": [{"recipient_party_id": "USCO", "share_bp": 8500},
                       {"recipient_party_id": "UP", "share_bp": 1500}],
        }],
    ))
    svc.sign_version("deal", "OUT-EU:v2")
    old_basis = svc.distribution_basis("fin", dist_us["distribution_id"])
    assert old_basis["rights_source"]["version_id"] == "OUT-EU:v1"
    event_v2 = svc.record_event("fin", {
        "event_id": "EV-2028-EU", "agreement_id": "OUT-EU", "kind": "royalty",
        "term_id": "eu_roy", "period": "2028", "region": "EU", "indication": "ONC",
        "face_amount": "60000000", "currency": "USD", "event_date": "2029-03-15",
        "source_note": "EUCO 2028 年度销售报告",
    })
    assert event_v2["version_id"] == "OUT-EU:v2"

    # 续约不得自动延长旧权利：在旧到期日前生效被阻断。
    svc.create_agreement("bd", {
        "agreement_id": "OUT-EU-R", "agreement_no": "LIC-2031-010", "candidate_id": "X-101",
        "direction": "outbound", "licensor_party_id": "USCO", "licensee_party_id": "EUCO",
        "parent_agreement_id": "IN-1", "renewal_of_agreement_id": "OUT-EU",
    })
    svc.draft_version("bd", "OUT-EU-R", _version(
        "2031-01-01", "2035-12-31", regions=("EU",), exclusive=True,
        sublicense={"scope": "none"}, obligations=downstream_obligations, terms=downstream_terms,
    ))
    review_renew_early = svc.run_review("bd", "OUT-EU-R:v1")
    assert any(i["code"] == "renewal_overlaps_prior_term" for i in review_renew_early["issues"])

    # 追溯：地区、候选药、授权链、未满足义务与审计哈希链。
    trace_eu = svc.trace_region("aud", "EU")
    chain = svc.authorization_chain("aud", "OUT-EU")
    unmet = svc.unmet_obligations("aud", "X-101")
    audit = svc.audit_chain("aud")
    assert audit["valid"] is True

    connection.close()
    return {
        "status": "ok",
        "upstream_review_blocking": review_in["blocking_count"],
        "downstream_signed": signed_in["state"] == "effective",
        "bad_draft_blocking": review_bad["blocking_count"],
        "bad_draft_sign_rejected": blocked_sign,
        "exclusivity_conflict_detected": [i["code"] for i in review_jp["issues"]],
        "forecast_inflows": len(forecast_a["inflows"]),
        "forecast_replayable": forecast_b["replayed"],
        "event_2027_state": frozen_view["state"],
        "settled_share_reverse_rejected": settle_protected,
        "frozen_share_confirm_rejected": confirm_blocked,
        "old_event_source_version": old_basis["rights_source"]["version_id"],
        "new_event_source_version": event_v2["version_id"],
        "renewal_early_blocked": [i["code"] for i in review_renew_early["issues"]],
        "region_trace_grants": len(trace_eu["grants"]),
        "rights_chain_order": chain["rights_chain_order"],
        "unmet_obligations": len(unmet["items"]),
        "audit_events": audit["events"],
        "audit_chain_valid": audit["valid"],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--workspace", default=".")
    parser.add_argument("--database", default=":memory:")
    args = parser.parse_args()
    print(json.dumps(run(args.database), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
