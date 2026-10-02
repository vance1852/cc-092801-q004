"""签署前条款评审：范围重叠、排他冲突、再许可越权与义务缺口。

全部为纯函数，输入“拟签版本 + 同候选药其他在谈/生效版本 + 权利来源链内容”，
输出结构化报告；blocking 非空时服务层拒绝签署。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from .domain import Scope, VersionContent


@dataclass(frozen=True, slots=True)
class ReviewIssue:
    code: str
    severity: str  # blocking | warning
    message: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "severity": self.severity, "message": self.message, "detail": self.detail}


@dataclass(frozen=True, slots=True)
class VersionContext:
    """同候选药下需要与拟签版本比对的其他版本（在谈及已生效）。"""

    agreement_id: str
    version_id: str
    state: str
    content: VersionContent
    exclusive_grants: frozenset[str] = frozenset()  # 该方已取得排他授权的来源链


def _windows_overlap(left: VersionContent, right: VersionContent) -> bool:
    """两个版本的有效区间 [effective_date, term_end_date] 是否在时间上相交。"""
    left_end = left.term_end_date
    right_end = right.term_end_date
    if left_end is not None and right.effective_date > left_end:
        return False
    if right_end is not None and left.effective_date > right_end:
        return False
    return True


@dataclass(frozen=True, slots=True)
class SourceLink:
    agreement_id: str
    version_id: str
    direction: str
    content: VersionContent


def _overlap_detail(left: VersionContent, right: VersionContent) -> dict[str, Any]:
    regions = sorted({
        token
        for a in left.scopes
        for b in right.scopes
        for token in (set(a.region_tokens()) | set(b.region_tokens()))
        if a.covers_region(token) and b.covers_region(token)
    })
    indications = sorted({
        token
        for a in left.scopes
        for b in right.scopes
        for token in set(a.indications) | set(b.indications)
        if a.covers_indication(token) and b.covers_indication(token)
    })
    stages = sorted({
        token
        for a in left.scopes
        for b in right.scopes
        for token in set(a.stages) | set(b.stages)
        if a.covers_stage(token) and b.covers_stage(token)
    })
    return {"regions": regions, "indications": indications, "stages": stages}


def review_version(
    candidate_content: VersionContent,
    candidate_version_id: str,
    siblings: Sequence[VersionContext],
    sources: Sequence[SourceLink],
    *,
    is_renewal: bool = False,
    previous_term_end: str | None = None,
    waived_obligation_ids: frozenset[str] | None = None,
) -> list[ReviewIssue]:
    waived = waived_obligation_ids or frozenset()
    issues: list[ReviewIssue] = []

    # 1) 范围重叠与排他冲突（与同候选药其他在谈/生效版本比对，且有效时间窗必须相交）。
    for sibling in siblings:
        if not _windows_overlap(candidate_content, sibling.content):
            continue
        for scope_index, scope in enumerate(candidate_content.scopes):
            for other_index, other_scope in enumerate(sibling.content.scopes):
                if not scope.overlaps(other_scope):
                    continue
                detail = {
                    "other_agreement_id": sibling.agreement_id,
                    "other_version_id": sibling.version_id,
                    "scope_index": scope_index,
                    "other_scope_index": other_index,
                    **_overlap_detail(candidate_content, sibling.content),
                }
                if scope.exclusive and other_scope.exclusive:
                    issues.append(ReviewIssue(
                        "exclusivity_conflict", "blocking",
                        f"与版本 {sibling.version_id} 在重叠范围内同时主张排他授权", detail,
                    ))
                else:
                    issues.append(ReviewIssue(
                        "scope_overlap", "blocking",
                        f"与版本 {sibling.version_id} 的授权范围发生重叠", detail,
                    ))

    # 2) 权利来源覆盖：拟授权的每个范围必须被来源链完整覆盖。
    if sources:
        for scope_index, scope in enumerate(candidate_content.scopes):
            if not any(_scope_covered_by(scope, source.content) for source in sources):
                issues.append(ReviewIssue(
                    "source_scope_gap", "blocking",
                    f"第 {scope_index + 1} 个授权范围没有被任何权利来源完整覆盖（无链授权）",
                    {"scope_index": scope_index, "scope": scope.to_dict()},
                ))

        # 3) 再许可范围不得超过上游授予（rank 与指名方双重约束）。
        max_rank = max((s.content.sublicense.rank for s in sources), default=0)
        upstream_named = frozenset().union(*(s.content.sublicense.named_parties for s in sources))
        sub = candidate_content.sublicense
        if sub.rank > max_rank:
            issues.append(ReviewIssue(
                "sublicense_exceeds_source", "blocking",
                f"再许可范围 {sub.scope} 超出上游最大授权 rank={max_rank}",
                {"requested": sub.scope, "upstream_max_rank": max_rank},
            ))
        if sub.scope == "named" and not set(sub.named_parties) <= upstream_named and max_rank < 3:
            unknown = sorted(set(sub.named_parties) - upstream_named)
            issues.append(ReviewIssue(
                "sublicense_party_not_granted", "blocking",
                "指名再许可方未出现在上游指名授权名单中",
                {"unknown_parties": unknown},
            ))

        # 4) 义务缺口：来源链中尚未到约定义务期限的必履义务必须在新版本中承接或标记豁免。
        carried = {o.obligation_id for o in candidate_content.obligations} | waived
        for source in sources:
            for obligation in source.content.obligations:
                if not obligation.required:
                    continue
                if obligation.obligation_id in carried:
                    continue
                if obligation.due_date < candidate_content.effective_date:
                    continue  # 旧版本期间已到期的义务由旧版本结算，不要求新版本承接
                issues.append(ReviewIssue(
                    "obligation_gap", "blocking",
                    f"来源版本 {source.version_id} 的义务 {obligation.obligation_id} 未在新版本承接",
                    {"source_agreement_id": source.agreement_id, "source_version_id": source.version_id,
                     "obligation_id": obligation.obligation_id, "due_date": obligation.due_date},
                ))

        # 5) 自身必履义务不得晚于版本到期日。
        if candidate_content.term_end_date:
            for obligation in candidate_content.obligations:
                if obligation.required and obligation.due_date > candidate_content.term_end_date:
                    issues.append(ReviewIssue(
                        "obligation_beyond_term", "warning",
                        f"义务 {obligation.obligation_id} 的到期日晚于版本期限",
                        {"obligation_id": obligation.obligation_id, "due_date": obligation.due_date,
                         "term_end_date": candidate_content.term_end_date},
                    ))

    # 6) 续约不得自动延长旧权利：续约版本最早只能在旧协议到期日次日生效，
    #    旧权利不会因续约谈判而延续；新期限独立起算。
    if is_renewal:
        if previous_term_end is None:
            issues.append(ReviewIssue(
                "renewal_without_term", "blocking",
                "续约所引用的旧协议没有到期日，无法证明旧权利已终止；不得自动延续旧权利",
            ))
        elif candidate_content.effective_date <= previous_term_end:
            issues.append(ReviewIssue(
                "renewal_overlaps_prior_term", "blocking",
                "续约版本在旧协议到期日之前生效，构成对旧权利的自动延长；续约只能在旧权利终止后开始",
                {"previous_term_end": previous_term_end,
                 "requested_effective_date": candidate_content.effective_date},
            ))

    return issues


def _scope_covered_by(scope: Scope, source: VersionContent) -> bool:
    """范围是否被某一来源版本的范围并集完整覆盖（含排他强度）。"""
    return any(source_scope.covers_scope(scope) for source_scope in source.scopes)


def blocking(issues: Sequence[ReviewIssue]) -> list[ReviewIssue]:
    return [issue for issue in issues if issue.severity == "blocking"]
