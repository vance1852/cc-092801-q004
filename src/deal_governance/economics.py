"""收益预测的可复算计算与沿授权链的瀑布分配。

预测只依赖条款快照与显式假设，纯函数、无 I/O：同样的快照与假设
必然得到同样的明细哈希。真实事件触发的分配同样在此纯函数中展开，
每行都带上计算公式与上游版本依据。
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .models import TermRevision

CENT = Decimal("0.01")
ONE = Decimal("1")


def _q(value: Decimal) -> Decimal:
    return value.quantize(CENT)


def build_projection_lines(term: TermRevision, assumptions: Mapping[str, Any]) -> list[dict[str, str]]:
    """根据条款快照与假设生成预测明细。

    assumptions:
      milestone_probabilities: {milestone_id: "0..1"}  每个里程碑必须显式给出
      annual_net_sales: {"2028": 金额, ...}            各年度净销售额假设
    """
    probs_raw = assumptions.get("milestone_probabilities", {})
    sales_raw = assumptions.get("annual_net_sales", {})
    if not isinstance(probs_raw, Mapping) or not isinstance(sales_raw, Mapping):
        raise ValueError("milestone_probabilities 与 annual_net_sales 必须是对象")

    prob_by_id: dict[str, Decimal] = {}
    for milestone in term.milestones:
        if milestone.milestone_id not in probs_raw:
            raise ValueError(f"缺少里程碑 {milestone.milestone_id} 的概率假设")
        raw = probs_raw[milestone.milestone_id]
        try:
            probability = Decimal(str(raw))
        except (InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(f"里程碑 {milestone.milestone_id} 概率必须是数值") from exc
        if not Decimal("0") <= probability <= ONE:
            raise ValueError(f"里程碑 {milestone.milestone_id} 概率必须在 0 到 1 之间")
        prob_by_id[milestone.milestone_id] = probability.quantize(Decimal("0.0001"))

    lines: list[dict[str, str]] = []
    if term.upfront_amount > 0:
        amount = str(term.upfront_amount)
        lines.append({
            "line_kind": "upfront",
            "ref_id": "upfront",
            "period_label": term.effective_date,
            "probability": "1.0000",
            "gross_amount": amount,
            "expected_amount": amount,
            "basis": "首付款，签署生效后按合同金额确认",
        })

    for milestone in term.milestones:
        probability = prob_by_id[milestone.milestone_id]
        lines.append({
            "line_kind": "milestone",
            "ref_id": milestone.milestone_id,
            "period_label": milestone.trigger_event,
            "probability": str(probability),
            "gross_amount": str(milestone.amount),
            "expected_amount": str(_q(milestone.amount * probability)),
            "basis": f"{milestone.milestone_type} 里程碑：{milestone.name}，金额×触发概率",
        })

    if term.royalty is not None:
        rate = term.royalty.rate
        for year in sorted(sales_raw):
            try:
                net_sales = _q(Decimal(str(sales_raw[year])))
            except Exception as exc:  # noqa: BLE001
                raise ValueError(f"annual_net_sales.{year} 必须是金额") from exc
            if net_sales < 0:
                raise ValueError(f"annual_net_sales.{year} 不能为负")
            lines.append({
                "line_kind": "royalty",
                "ref_id": "royalty",
                "period_label": str(year),
                "probability": "1.0000",
                "gross_amount": str(_q(net_sales * rate)),
                "expected_amount": str(_q(net_sales * rate)),
                "basis": f"净销售额 {net_sales} × 分成比例 {rate}",
            })

    return lines


def projection_total(lines: list[Mapping[str, str]]) -> Decimal:
    return _q(sum((Decimal(line["expected_amount"]) for line in lines), Decimal("0")))


def _snapshot(row: Mapping[str, Any]) -> Mapping[str, Any]:
    import json
    return json.loads(row["snapshot_json"])


def waterfall_allocations(
    event_revision_row: Mapping[str, Any],
    ancestor_rows: list[Mapping[str, Any]],
    payment_kind: str,
    gross_amount: Decimal,
    basis_amount: Decimal | None,
    period_label: str,
) -> list[dict[str, Any]]:
    """把一笔真实收款沿授权链展开为分配行（尚未落库、无 id）。

    - 上游/无链：upfront、milestone 形成 direct 行；royalty 按本版本比例形成 direct 行。
    - 再许可链上的 upfront/milestone：每一级按上游 sublicense_income_share
      向上形成 passthrough，本级留下 retained 记账行。
    - 再许可链上的 royalty：每个上游版本按其快照比例对同一净销售额形成 passthrough。
    """
    import json

    result: list[dict[str, Any]] = []
    current_snapshot = json.loads(event_revision_row["snapshot_json"])
    current_revision_id = event_revision_row["revision_id"]
    currency = None  # 金额本身无货币，货币由事件行携带

    if payment_kind == "royalty":
        rate = Decimal(current_snapshot["royalty"]["rate"])
        basis = basis_amount or Decimal("0")
        result.append({
            "revision_id": current_revision_id,
            "upstream_revision_id": None,
            "recipient_party_id": current_snapshot["licensor_party_id"],
            "flow_role": "direct",
            "amount": str(_q(basis * rate)),
            "state": "pending",
            "period_label": period_label,
            "basis": {
                "rule": "royalty_direct",
                "formula": f"{_q(basis)} × {rate}",
                "basis_amount": str(_q(basis)),
                "rate": str(rate),
                "revision_id": current_revision_id,
            },
        })
        for ancestor in ancestor_rows:
            snap = json.loads(ancestor["snapshot_json"])
            if not snap.get("royalty"):
                continue
            up_rate = Decimal(snap["royalty"]["rate"])
            result.append({
                "revision_id": current_revision_id,
                "upstream_revision_id": ancestor["revision_id"],
                "recipient_party_id": snap["licensor_party_id"],
                "flow_role": "passthrough",
                "amount": str(_q(basis * up_rate)),
                "state": "pending",
                "period_label": period_label,
                "basis": {
                    "rule": "royalty_upstream",
                    "formula": f"{_q(basis)} × {up_rate}",
                    "basis_amount": str(_q(basis)),
                    "rate": str(up_rate),
                    "source_revision_id": ancestor["revision_id"],
                },
            })
        return result

    if not ancestor_rows:
        result.append({
            "revision_id": current_revision_id,
            "upstream_revision_id": None,
            "recipient_party_id": current_snapshot["licensor_party_id"],
            "flow_role": "direct",
            "amount": str(_q(gross_amount)),
            "state": "pending",
            "period_label": period_label,
            "basis": {
                "rule": "direct",
                "payment_kind": payment_kind,
                "gross_amount": str(_q(gross_amount)),
                "revision_id": current_revision_id,
            },
        })
        return result

    # 再许可链上的首付款/里程碑：逐级向上分成
    received = _q(gross_amount)
    current_snap = current_snapshot
    current_id = current_revision_id
    for ancestor in ancestor_rows:
        up_snap = json.loads(ancestor["snapshot_json"])
        share = Decimal(up_snap["sublicense_income_share"])
        passed = _q(received * share)
        retained = _q(received - passed)
        if retained > 0:
            result.append({
                "revision_id": current_id,
                "upstream_revision_id": ancestor["revision_id"],
                "recipient_party_id": current_snap["licensor_party_id"],
                "flow_role": "retained",
                "amount": str(retained),
                "state": "settled",  # 留存仅记账，不进入可支付/可冻结队列
                "period_label": period_label,
                "basis": {
                    "rule": "sublicense_retained",
                    "formula": f"{received} − {received} × {share}",
                    "received": str(received),
                    "share": str(share),
                    "source_revision_id": ancestor["revision_id"],
                },
            })
        result.append({
            "revision_id": current_id,
            "upstream_revision_id": ancestor["revision_id"],
            "recipient_party_id": up_snap["licensor_party_id"],
            "flow_role": "passthrough",
            "amount": str(passed),
            "state": "pending",
            "period_label": period_label,
            "basis": {
                "rule": "sublicense_passthrough",
                "formula": f"{received} × {share}",
                "received": str(received),
                "share": str(share),
                "source_revision_id": ancestor["revision_id"],
            },
        })
        received = passed
        current_snap = up_snap
        current_id = ancestor["revision_id"]
    return result
