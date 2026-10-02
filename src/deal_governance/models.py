"""候选药、授权范围、义务与收益条款的领域输入契约。

所有货币、比例与里程碑金额都以 Decimal 文本保存，避免浮点误差导致
预测不可复算。条款版本（TermRevision）是一次修订的完整不可变快照。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .clock import date_text

IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}$")

EXCLUSIVITY = {"exclusive", "sole", "non-exclusive"}
SUBLICENSE_SCOPES = {"none", "same-scope", "negotiate"}
DEVELOPMENT_STAGES = {"preclinical", "phase1", "phase2", "phase3", "filing", "approved"}
OBLIGATION_KINDS = {"development", "regulatory", "commercial", "reporting", "payment"}
PAYMENT_KINDS = {"upfront", "milestone", "royalty", "sublicense", "reimbursement"}
MILESTONE_TYPES = {
    "development", "regulatory", "commercial", "sales",
}  # 销售里程碑按 net_sales 触发，其余按事件确认


def required_text(value: object, name: str, maximum: int = 128) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 不能为空")
    text = value.strip()
    if len(text) > maximum:
        raise ValueError(f"{name} 不能超过 {maximum} 个字符")
    return text


def identifier(value: object, name: str) -> str:
    text = required_text(value, name, 64)
    if not IDENTIFIER.fullmatch(text):
        raise ValueError(f"{name} 格式不正确")
    return text


def money(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{name} 必须是十进制数值") from exc
    if not result.is_finite() or result < 0:
        raise ValueError(f"{name} 必须是非负有限数值")
    return result.quantize(Decimal("0.01"))


def ratio(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{name} 必须是十进制数值") from exc
    if not result.is_finite() or not Decimal("0") <= result <= Decimal("1"):
        raise ValueError(f"{name} 必须在 0 到 1 之间")
    return result.quantize(Decimal("0.0001"))


def code_list(value: object, name: str, allowed: set[str] | None = None) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"{name} 至少包含一项")
    result: list[str] = []
    for item in value:
        text = required_text(item, f"{name} 元素", 64)
        if allowed is not None and text not in allowed:
            raise ValueError(f"{name} 含不支持的值: {text}")
        if text not in result:
            result.append(text)
    return result


@dataclass(frozen=True, slots=True)
class Candidate:
    candidate_id: str
    name: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Candidate":
        return cls(identifier(raw.get("candidate_id"), "candidate_id"),
                   required_text(raw.get("name"), "name"))


@dataclass(frozen=True, slots=True)
class Party:
    party_id: str
    name: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Party":
        return cls(identifier(raw.get("party_id"), "party_id"),
                   required_text(raw.get("name"), "name"))


@dataclass(frozen=True, slots=True)
class Scope:
    """授权范围：地区 × 适应症 × 开发阶段 三维集合。"""

    territories: tuple[str, ...]
    indications: tuple[str, ...]
    stages: tuple[str, ...]

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Scope":
        if not isinstance(raw, Mapping):
            raise ValueError("scope 必须是对象")
        return cls(
            tuple(code_list(raw.get("territories"), "territories")),
            tuple(code_list(raw.get("indications"), "indications")),
            tuple(code_list(raw.get("stages"), "stages", DEVELOPMENT_STAGES)),
        )

    def overlaps(self, other: "Scope") -> bool:
        return bool(
            set(self.territories) & set(other.territories)
            and set(self.indications) & set(other.indications)
            and set(self.stages) & set(other.stages)
        )

    def overlap_detail(self, other: "Scope") -> dict[str, list[str]]:
        return {
            "territories": sorted(set(self.territories) & set(other.territories)),
            "indications": sorted(set(self.indications) & set(other.indications)),
            "stages": sorted(set(self.stages) & set(other.stages)),
        }

    def contains(self, other: "Scope") -> bool:
        return (
            set(other.territories) <= set(self.territories)
            and set(other.indications) <= set(self.indications)
            and set(other.stages) <= set(self.stages)
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "territories": list(self.territories),
            "indications": list(self.indications),
            "stages": list(self.stages),
        }


@dataclass(frozen=True, slots=True)
class Obligation:
    """共同开发/合规义务；due_event 与 due_date 至少填一个。"""

    obligation_id: str
    kind: str
    description: str
    due_date: str | None
    due_event: str | None
    owner_party_id: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Obligation":
        kind = required_text(raw.get("kind"), "kind", 32)
        if kind not in OBLIGATION_KINDS:
            raise ValueError(f"不支持的义务类型: {kind}")
        due_date = raw.get("due_date")
        due_event = raw.get("due_event")
        due_date_text = date_text(due_date, "due_date") if due_date else None
        due_event_text = required_text(due_event, "due_event", 128) if due_event else None
        if due_date_text is None and due_event_text is None:
            raise ValueError("due_date 与 due_event 至少填写一项")
        return cls(
            identifier(raw.get("obligation_id"), "obligation_id"),
            kind,
            required_text(raw.get("description"), "description", 512),
            due_date_text,
            due_event_text,
            identifier(raw.get("owner_party_id"), "owner_party_id"),
        )


@dataclass(frozen=True, slots=True)
class MilestoneTerm:
    milestone_id: str
    milestone_type: str
    name: str
    amount: Decimal
    trigger_event: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "MilestoneTerm":
        mtype = required_text(raw.get("milestone_type"), "milestone_type", 32)
        if mtype not in MILESTONE_TYPES:
            raise ValueError(f"不支持的里程碑类型: {mtype}")
        return cls(
            identifier(raw.get("milestone_id"), "milestone_id"),
            mtype,
            required_text(raw.get("name"), "name"),
            money(raw.get("amount"), "amount"),
            required_text(raw.get("trigger_event"), "trigger_event", 256),
        )


@dataclass(frozen=True, slots=True)
class RoyaltyTerm:
    rate: Decimal
    cap_rate: Decimal | None = None

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "RoyaltyTerm":
        if not isinstance(raw, Mapping):
            raise ValueError("royalty 必须是对象")
        cap = raw.get("cap_rate")
        cap_rate = ratio(cap, "cap_rate") if cap is not None else None
        rate = ratio(raw.get("rate"), "rate")
        if cap_rate is not None and rate > cap_rate:
            raise ValueError("royalty.rate 不能超过 cap_rate")
        return cls(rate, cap_rate)


@dataclass(frozen=True, slots=True)
class TermRevision:
    """一次条款修订的完整快照。"""

    revision_no: int
    effective_date: str
    expiry_date: str | None
    licensor_party_id: str
    licensee_party_id: str
    scope: Scope
    exclusivity: str
    sublicense_scope: str
    upfront_amount: Decimal
    milestones: tuple[MilestoneTerm, ...]
    royalty: RoyaltyTerm | None
    sublicense_income_share: Decimal
    obligations: tuple[Obligation, ...]
    source_revision_id: str | None
    change_note: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any], revision_no: int) -> "TermRevision":
        exclusivity = required_text(raw.get("exclusivity"), "exclusivity", 16)
        if exclusivity not in EXCLUSIVITY:
            raise ValueError("exclusivity 必须是 exclusive、sole 或 non-exclusive")
        sublicense_scope = required_text(raw.get("sublicense_scope"), "sublicense_scope", 16)
        if sublicense_scope not in SUBLICENSE_SCOPES:
            raise ValueError("sublicense_scope 必须是 none、same-scope 或 negotiate")
        effective = date_text(raw.get("effective_date"), "effective_date")
        expiry = raw.get("expiry_date")
        expiry_text = date_text(expiry, "expiry_date") if expiry else None
        if expiry_text and expiry_text < effective:
            raise ValueError("expiry_date 不能早于 effective_date")
        raw_milestones = raw.get("milestones", [])
        if not isinstance(raw_milestones, list):
            raise ValueError("milestones 必须是数组")
        raw_obligations = raw.get("obligations", [])
        if not isinstance(raw_obligations, list):
            raise ValueError("obligations 必须是数组")
        source = raw.get("source_revision_id")
        return cls(
            revision_no=revision_no,
            effective_date=effective,
            expiry_date=expiry_text,
            licensor_party_id=identifier(raw.get("licensor_party_id"), "licensor_party_id"),
            licensee_party_id=identifier(raw.get("licensee_party_id"), "licensee_party_id"),
            scope=Scope.from_dict(raw.get("scope", {})),
            exclusivity=exclusivity,
            sublicense_scope=sublicense_scope,
            upfront_amount=money(raw.get("upfront_amount", 0), "upfront_amount"),
            milestones=tuple(MilestoneTerm.from_dict(m) for m in raw_milestones),
            royalty=RoyaltyTerm.from_dict(raw["royalty"]) if raw.get("royalty") else None,
            sublicense_income_share=ratio(
                raw.get("sublicense_income_share", 0), "sublicense_income_share"
            ),
            obligations=tuple(Obligation.from_dict(o) for o in raw_obligations),
            source_revision_id=identifier(source, "source_revision_id") if source else None,
            change_note=required_text(raw.get("change_note", "初始版本"), "change_note", 512),
        )

    def payment_obligated_upfront(self) -> bool:
        return self.upfront_amount > 0
