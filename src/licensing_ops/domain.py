"""版本化海外授权与收益台账的领域模型与纯函数。

一份“条款修订版本”（VersionContent）完整描述某一时点有效的：
授权范围（地区/适应症/开发阶段/排他）、再许可范围、共同开发义务与经济条款。
所有金额使用 Decimal 文本，分成以基点（bp，10000=100%）表达。
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Any, Iterable, Mapping, Sequence

# 标准地区码：ROW 按“标准地区减去明确排除项”解释，WORLD 覆盖一切。
STANDARD_REGIONS = frozenset({"NA", "LATAM", "EU", "UK", "CN", "JP", "APAC", "MEA", "ROW"})
WIDE_REGIONS = frozenset({"WORLD", "ROW"})
INDICATION_ANY = "ANY"
STAGE_ANY = "ANY"
STAGES = frozenset({
    "discovery", "preclinical", "ind", "phase1", "phase2", "phase3",
    "regulatory_filing", "approval", "commercial",
})
SUBLICENSE_RANKS = {"none": 0, "affiliates": 1, "named": 2, "any": 3}
TERM_KINDS = frozenset({"upfront", "milestone", "royalty"})
MONEY_Q = Decimal("0.01")
BP_TOTAL = 10000


# ---------------------------------------------------------------- 基础校验

def _text(value: object, field_name: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValueError(f"{field_name} 不能超过 {maximum} 个字符")
    return result


def _identifier(value: object, field_name: str) -> str:
    result = _text(value, field_name, 64)
    if not all(ch.isalnum() or ch in "_.:-" for ch in result) or not result[0].isalnum():
        raise ValueError(f"{field_name} 只能包含字母数字与 _.:- 且以字母数字开头")
    return result


def _money(value: object, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{field_name} 必须是十进制数值") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{field_name} 必须是非负有限数值")
    return result


def _rate(value: object, field_name: str) -> Decimal:
    result = _money(value, field_name)
    if result > 100:
        raise ValueError(f"{field_name} 不能超过 100")
    return result


def _date_text(value: object, field_name: str) -> str:
    result = _text(value, field_name, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field_name} 必须是 YYYY-MM-DD 日期") from exc


def _probability(value: object, field_name: str) -> Decimal:
    result = _money(value, field_name)
    if result > 1:
        raise ValueError(f"{field_name} 必须在 0 到 1 之间")
    return result


def money_text(value: Decimal) -> str:
    return format(value.quantize(MONEY_Q, rounding=ROUND_HALF_UP), "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


# ---------------------------------------------------------------- 授权范围

@dataclass(frozen=True, slots=True)
class Scope:
    regions: tuple[str, ...]
    region_excludes: tuple[str, ...]
    indications: tuple[str, ...]
    stages: tuple[str, ...]
    exclusive: bool
    therapy_field: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Scope":
        if not isinstance(raw, Mapping):
            raise ValueError("scopes 必须是对象数组")
        regions = tuple(sorted({_text(r, "region", 16).upper() for r in raw.get("regions", [])}))
        if not regions:
            raise ValueError("scope.regions 不能为空")
        excludes = tuple(sorted({_text(r, "region_excludes 项", 16).upper() for r in raw.get("region_excludes", [])}))
        indications = tuple(sorted({_text(i, "indication", 32).upper() for i in raw.get("indications", [])}))
        if not indications:
            raise ValueError("scope.indications 不能为空")
        stages_raw = raw.get("stages", [])
        stages = tuple(sorted({
            STAGE_ANY if _text(s, "stage", 32).lower() == STAGE_ANY.lower()
            else _text(s, "stage", 32).lower()
            for s in stages_raw
        }))
        if not stages or any(s != STAGE_ANY and s not in STAGES for s in stages):
            raise ValueError(f"stage 取值非法，可选：{sorted(STAGES)} 或 {STAGE_ANY}")
        therapy_field = _text(raw.get("therapy_field", INDICATION_ANY), "therapy_field", 32).upper()
        return cls(regions, excludes, indications, stages, bool(raw.get("exclusive", False)), therapy_field)

    def to_dict(self) -> dict[str, Any]:
        return {
            "regions": list(self.regions),
            "region_excludes": list(self.region_excludes),
            "indications": list(self.indications),
            "stages": list(self.stages),
            "exclusive": self.exclusive,
            "therapy_field": self.therapy_field,
        }

    def covers_region(self, token: str) -> bool:
        token = token.upper()
        if token in self.regions:
            return True
        if "WORLD" in self.regions and token not in self.region_excludes:
            return True
        if "ROW" in self.regions and token in STANDARD_REGIONS and token not in self.region_excludes:
            return True
        return False

    def covers_indication(self, token: str) -> bool:
        token = token.upper()
        return INDICATION_ANY in self.indications or token in self.indications

    def covers_stage(self, token: str) -> bool:
        token = token.lower()
        return STAGE_ANY in self.stages or token in self.stages

    def covers_field(self, token: str) -> bool:
        token = token.upper()
        return self.therapy_field == INDICATION_ANY or self.therapy_field == token

    def region_tokens(self) -> frozenset[str]:
        tokens: set[str] = set(self.regions)
        if "WORLD" in self.regions:
            tokens |= STANDARD_REGIONS
        if "ROW" in self.regions:
            tokens |= STANDARD_REGIONS
        return frozenset(t for t in tokens if t not in self.region_excludes or t in self.regions)

    def overlaps(self, other: "Scope") -> bool:
        region_candidates = set(self.regions) | set(other.regions) | STANDARD_REGIONS
        region_ok = any(self.covers_region(t) and other.covers_region(t) for t in region_candidates)
        if not region_ok:
            return False
        indication_ok = any(
            self.covers_indication(i) and other.covers_indication(i)
            for i in set(self.indications) | set(other.indications) | {INDICATION_ANY}
        )
        if not indication_ok:
            return False
        stage_ok = any(
            self.covers_stage(s) and other.covers_stage(s)
            for s in set(self.stages) | set(other.stages) | {STAGE_ANY}
        )
        if not stage_ok:
            return False
        field_ok = self.covers_field(other.therapy_field) or other.covers_field(self.therapy_field)
        return bool(field_ok)

    def covers_scope(self, other: "Scope") -> bool:
        """上游范围是否完整覆盖下游范围（用于权利来源核验）。"""
        if other.exclusive and not self.exclusive:
            return False
        if not all(self.covers_region(r) for r in other.region_tokens()):
            return False
        if not all(self.covers_indication(i) for i in other.indications if i != INDICATION_ANY):
            return False
        if not all(self.covers_stage(s) for s in other.stages if s != STAGE_ANY):
            return False
        if other.therapy_field != INDICATION_ANY and not self.covers_field(other.therapy_field):
            return False
        return True


# ---------------------------------------------------------------- 再许可与义务

@dataclass(frozen=True, slots=True)
class Sublicense:
    scope: str
    rank: int
    named_parties: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any] | None) -> "Sublicense":
        raw = raw or {"scope": "none"}
        scope = _text(raw.get("scope"), "sublicense.scope", 16).lower()
        if scope not in SUBLICENSE_RANKS:
            raise ValueError(f"sublicense.scope 必须是 {sorted(SUBLICENSE_RANKS)}")
        named = tuple(sorted({_identifier(p, "sublicense.named_parties 项") for p in raw.get("named_parties", [])}))
        if scope == "named" and not named:
            raise ValueError("sublicense.scope 为 named 时必须提供 named_parties")
        return cls(scope, SUBLICENSE_RANKS[scope], named)

    def to_dict(self) -> dict[str, Any]:
        return {"scope": self.scope, "rank": self.rank, "named_parties": list(self.named_parties)}


@dataclass(frozen=True, slots=True)
class Obligation:
    obligation_id: str
    kind: str
    description: str
    assignee_party_id: str
    due_date: str
    required: bool

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Obligation":
        return cls(
            _identifier(raw.get("obligation_id"), "obligation_id"),
            _text(raw.get("kind"), "obligation.kind", 32).lower(),
            _text(raw.get("description"), "obligation.description", 512),
            _identifier(raw.get("assignee_party_id"), "assignee_party_id"),
            _date_text(raw.get("due_date"), "obligation.due_date"),
            bool(raw.get("required", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "obligation_id": self.obligation_id,
            "kind": self.kind,
            "description": self.description,
            "assignee_party_id": self.assignee_party_id,
            "due_date": self.due_date,
            "required": self.required,
        }


# ---------------------------------------------------------------- 经济条款

@dataclass(frozen=True, slots=True)
class Share:
    recipient_party_id: str
    share_bp: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Share":
        bp = raw.get("share_bp")
        if isinstance(bp, bool) or not isinstance(bp, int) or not 0 < bp <= BP_TOTAL:
            raise ValueError("share_bp 必须是 1 到 10000 的整数")
        return cls(_identifier(raw.get("recipient_party_id"), "recipient_party_id"), bp)

    def to_dict(self) -> dict[str, Any]:
        return {"recipient_party_id": self.recipient_party_id, "share_bp": self.share_bp}


@dataclass(frozen=True, slots=True)
class RoyaltyTier:
    up_to: Decimal | None  # 本级封顶净销售额；None 表示顶格
    rate_percent: Decimal

    def to_dict(self) -> dict[str, Any]:
        return {"up_to": None if self.up_to is None else money_text(self.up_to), "rate_percent": money_text(self.rate_percent)}


@dataclass(frozen=True, slots=True)
class Term:
    term_id: str
    kind: str
    label: str
    amount: Decimal | None
    currency: str
    due_date: str | None
    trigger_stage: str | None
    tiers: tuple[RoyaltyTier, ...]
    shares: tuple[Share, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], currency: str) -> "Term":
        kind = _text(raw.get("kind"), "term.kind", 16).lower()
        if kind not in TERM_KINDS:
            raise ValueError(f"term.kind 必须是 {sorted(TERM_KINDS)}")
        term_id = _identifier(raw.get("term_id"), "term_id")
        term_currency = _text(raw.get("currency", currency), "currency", 8).upper()
        if term_currency != currency:
            raise ValueError(f"term {term_id} 币种必须与版本币种 {currency} 一致")
        shares = tuple(Share.from_dict(item) for item in raw.get("shares", []))
        if not shares:
            raise ValueError(f"term {term_id} 必须至少有一个收款分成方")
        recipients = [s.recipient_party_id for s in shares]
        if len(recipients) != len(set(recipients)):
            raise ValueError(f"term {term_id} 的收款方重复")
        if sum(s.share_bp for s in shares) != BP_TOTAL:
            raise ValueError(f"term {term_id} 的分成基点之和必须等于 10000")
        amount = None
        due_date = None
        trigger_stage = None
        tiers: tuple[RoyaltyTier, ...] = ()
        if kind in {"upfront", "milestone"}:
            amount = _money(raw.get("amount"), f"term {term_id}.amount")
            if kind == "upfront":
                due_date = _date_text(raw.get("due_date"), f"term {term_id}.due_date")
            else:
                trigger_stage = _text(raw.get("trigger_stage"), f"term {term_id}.trigger_stage", 32).lower()
        else:
            tier_raw = raw.get("tiers")
            if not isinstance(tier_raw, list) or not tier_raw:
                raise ValueError(f"term {term_id} 必须提供 tiers 分级费率")
            parsed: list[RoyaltyTier] = []
            for item in tier_raw:
                cap = item.get("up_to")
                parsed.append(RoyaltyTier(
                    None if cap is None else _money(cap, "tier.up_to"),
                    _rate(item.get("rate_percent"), "tier.rate_percent"),
                ))
            for index, tier in enumerate(parsed):
                if tier.up_to is None and index != len(parsed) - 1:
                    raise ValueError(f"term {term_id} 顶格费率档只能位于最后")
            for earlier, later in zip(parsed, parsed[1:]):
                if earlier.up_to is None:
                    raise ValueError(f"term {term_id} 顶格费率档只能位于最后")
                if later.up_to is not None and earlier.up_to >= later.up_to:
                    raise ValueError(f"term {term_id} 的 tier.up_to 必须严格递增")
            tiers = tuple(parsed)
        return cls(
            term_id, kind, _text(raw.get("label", ""), "term.label", 128),
            amount, term_currency, due_date, trigger_stage, tiers, shares,
        )

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "term_id": self.term_id,
            "kind": self.kind,
            "label": self.label,
            "currency": self.currency,
            "shares": [s.to_dict() for s in self.shares],
        }
        if self.kind in {"upfront", "milestone"}:
            data["amount"] = money_text(self.amount)  # type: ignore[arg-type]
            if self.due_date:
                data["due_date"] = self.due_date
            if self.trigger_stage:
                data["trigger_stage"] = self.trigger_stage
        else:
            data["tiers"] = [t.to_dict() for t in self.tiers]
        return data

    def royalty_amount(self, net_sales: Decimal) -> Decimal:
        """按超额累进费率档计算特许使用费（销售分成）。"""
        if self.kind != "royalty":
            raise ValueError("只有 royalty 条款可以计算销售分成")
        if net_sales <= 0:
            return Decimal("0.00")
        amount = Decimal("0")
        lower = Decimal("0")
        for tier in self.tiers:
            bracket = net_sales - lower if tier.up_to is None else min(net_sales, tier.up_to) - lower
            if bracket > 0:
                amount += bracket * tier.rate_percent / 100
            if tier.up_to is None or net_sales <= tier.up_to:
                break
            lower = tier.up_to
        return amount.quantize(MONEY_Q, rounding=ROUND_HALF_UP)


# ---------------------------------------------------------------- 版本内容

@dataclass(frozen=True, slots=True)
class VersionContent:
    effective_date: str
    term_end_date: str | None
    currency: str
    scopes: tuple[Scope, ...]
    sublicense: Sublicense
    obligations: tuple[Obligation, ...]
    terms: tuple[Term, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "VersionContent":
        if not isinstance(raw, Mapping):
            raise ValueError("版本内容必须是对象")
        effective_date = _date_text(raw.get("effective_date"), "effective_date")
        term_end = raw.get("term_end_date")
        term_end_date = None if term_end is None else _date_text(term_end, "term_end_date")
        if term_end_date is not None and term_end_date < effective_date:
            raise ValueError("term_end_date 不能早于 effective_date")
        currency = _text(raw.get("currency", "USD"), "currency", 8).upper()
        scopes = tuple(Scope.from_dict(item) for item in raw.get("scopes", []))
        if not scopes:
            raise ValueError("版本至少需要一个授权范围")
        terms = tuple(Term.from_dict(item, currency) for item in raw.get("terms", []))
        if not terms:
            raise ValueError("版本至少需要一个经济条款")
        ids = [t.term_id for t in terms]
        if len(ids) != len(set(ids)):
            raise ValueError("term_id 不能重复")
        if sum(1 for t in terms if t.kind == "royalty") > 1:
            raise ValueError("一个版本至多包含一个 royalty 条款；多来源堆叠应体现为多份协议")
        obligations = tuple(Obligation.from_dict(item) for item in raw.get("obligations", []))
        obligation_ids = [o.obligation_id for o in obligations]
        if len(obligation_ids) != len(set(obligation_ids)):
            raise ValueError("obligation_id 不能重复")
        return cls(
            effective_date, term_end_date, currency, scopes,
            Sublicense.from_dict(raw.get("sublicense")), obligations, terms,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "effective_date": self.effective_date,
            "term_end_date": self.term_end_date,
            "currency": self.currency,
            "scopes": [s.to_dict() for s in self.scopes],
            "sublicense": self.sublicense.to_dict(),
            "obligations": [o.to_dict() for o in self.obligations],
            "terms": [t.to_dict() for t in self.terms],
        }

    def term(self, term_id: str) -> Term | None:
        return next((t for t in self.terms if t.term_id == term_id), None)

    def covers_sale(self, region: str, indication: str) -> bool:
        return any(
            s.covers_region(region) and s.covers_indication(indication) and s.covers_stage("commercial")
            for s in self.scopes
        )

    def content_sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.to_dict()).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 分成拆分

def split_amount(gross: Decimal, shares: Sequence[Share]) -> list[Decimal]:
    """按基点拆分金额，尾差（分）按分成比例顺序补给最大方，保证合计严格等于 gross。"""
    total_bp = sum(s.share_bp for s in shares)
    cents_total = int((gross * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    allocated: list[int] = []
    for share in shares:
        allocated.append(int((Decimal(cents_total) * share.share_bp / total_bp).to_integral_value(rounding="ROUND_FLOOR")))
    remainder = cents_total - sum(allocated)
    order = sorted(range(len(shares)), key=lambda i: (-shares[i].share_bp, shares[i].recipient_party_id))
    for index in order[:remainder]:
        allocated[index] += 1
    return [Decimal(cents) / 100 for cents in allocated]


# ---------------------------------------------------------------- 收益预测假设

@dataclass(frozen=True, slots=True)
class SalesAssumption:
    period: str            # 年份，如 2027
    region: str
    indication: str
    net_sales: Decimal

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SalesAssumption":
        period = _text(raw.get("period"), "sales.period", 8)
        if len(period) != 4 or not period.isdigit():
            raise ValueError("sales.period 必须是 YYYY 年份")
        return cls(
            period,
            _text(raw.get("region"), "sales.region", 16).upper(),
            _text(raw.get("indication"), "sales.indication", 32).upper(),
            _money(raw.get("net_sales"), "sales.net_sales"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"period": self.period, "region": self.region, "indication": self.indication, "net_sales": money_text(self.net_sales)}


@dataclass(frozen=True, slots=True)
class AssumptionSet:
    assumptions_id: str
    currency: str
    horizon_end: str
    sales: tuple[SalesAssumption, ...]
    milestone_probability: Mapping[str, Decimal]

    @classmethod
    def from_dict(cls, assumptions_id: str, raw: Mapping[str, Any]) -> "AssumptionSet":
        currency = _text(raw.get("currency", "USD"), "currency", 8).upper()
        horizon_end = _date_text(raw.get("horizon_end"), "horizon_end")
        sales = tuple(SalesAssumption.from_dict(item) for item in raw.get("sales", []))
        probabilities = {
            _identifier(item.get("term_id"), "milestone.term_id"):
                _probability(item.get("probability"), "milestone.probability")
            for item in raw.get("milestones", [])
        }
        return cls(assumptions_id, currency, horizon_end, sales, probabilities)

    def content_dict(self) -> dict[str, Any]:
        return {
            "currency": self.currency,
            "horizon_end": self.horizon_end,
            "sales": [s.to_dict() for s in self.sales],
            "milestones": [
                {"term_id": tid, "probability": format(p, "f")}
                for tid, p in sorted(self.milestone_probability.items())
            ],
        }

    def content_sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.content_dict()).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ProjectedInflow:
    agreement_id: str
    version_id: str
    term_id: str
    kind: str
    period: str
    region: str | None
    indication: str | None
    gross: Decimal
    currency: str
    basis: dict[str, Any]


def project_inflows(
    agreement_id: str,
    version_id: str,
    content: VersionContent,
    assumptions: AssumptionSet,
    as_of: str,
    window_end: str | None = None,
) -> list[ProjectedInflow]:
    """纯函数：在 [as_of, min(版本到期, 假设截止)] 窗口内按假设生成预期流入。

    不读取数据库；调用方负责挑选每个时点“当时有效”的版本。
    """
    horizon_end = min(assumptions.horizon_end, window_end or assumptions.horizon_end)
    if content.effective_date > horizon_end:
        return []
    inflows: list[ProjectedInflow] = []
    for term in content.terms:
        if term.currency != assumptions.currency:
            continue
        if term.kind == "upfront" and term.due_date and as_of <= term.due_date <= horizon_end:
            inflows.append(ProjectedInflow(
                agreement_id, version_id, term.term_id, "upfront", term.due_date[:7],
                None, None, term.amount, term.currency,  # type: ignore[arg-type]
                {"type": "upfront", "due_date": term.due_date, "probability": "1"},
            ))
        elif term.kind == "milestone":
            probability = assumptions.milestone_probability.get(term.term_id)
            if probability is not None and probability > 0:
                expected = (term.amount * probability).quantize(MONEY_Q, rounding=ROUND_HALF_UP)  # type: ignore[operator]
                inflows.append(ProjectedInflow(
                    agreement_id, version_id, term.term_id, "milestone", as_of[:7],
                    None, None, expected, term.currency,
                    {"type": "milestone", "trigger_stage": term.trigger_stage,
                     "probability": format(probability, "f"), "face_amount": money_text(term.amount)},  # type: ignore[arg-type]
                ))
        elif term.kind == "royalty":
            for sale in assumptions.sales:
                year_start = f"{sale.period}-01-01"
                if not (as_of <= year_start <= horizon_end and content.effective_date <= year_start):
                    continue
                if not content.covers_sale(sale.region, sale.indication):
                    continue
                royalty = term.royalty_amount(sale.net_sales)
                if royalty > 0:
                    inflows.append(ProjectedInflow(
                        agreement_id, version_id, term.term_id, "royalty", sale.period,
                        sale.region, sale.indication, royalty, term.currency,
                        {"type": "royalty", "net_sales": money_text(sale.net_sales)},
                    ))
    inflows.sort(key=lambda x: (x.period, x.term_id, x.region or "", x.indication or ""))
    return inflows
