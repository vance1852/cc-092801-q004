from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from licensing_ops.clock import FrozenClock
from licensing_ops.domain import (
    Scope,
    Sublicense,
    VersionContent,
    split_amount,
)
from licensing_ops.errors import (
    Conflict,
    Forbidden,
    InvalidState,
    NotFound,
    ReviewBlocked,
    ValidationFailed,
)
from licensing_ops.ledger_api import LedgerApplication, Response
from licensing_ops.review import SourceLink, VersionContext, review_version
from licensing_ops.service_ledger import LicensingService


# --------------------------------------------------------------------- 构造辅助

def scope(regions=("WORLD",), exclusive=True, indications=("ONC",), stages=("ANY",), excludes=()):
    return {
        "regions": list(regions), "region_excludes": list(excludes),
        "indications": list(indications), "stages": list(stages),
        "exclusive": exclusive, "therapy_field": "ANY",
    }


def royalty(term_id="roy", tiers=None, shares=None):
    return {
        "term_id": term_id, "kind": "royalty", "label": "分成",
        "tiers": tiers or [{"up_to": None, "rate_percent": "10"}],
        "shares": shares or [{"recipient_party_id": "UP", "share_bp": 10000}],
    }


def upfront(amount="1000", due="2026-11-01", payee="UP"):
    return {"term_id": "upfront", "kind": "upfront", "label": "首付",
            "amount": amount, "due_date": due,
            "shares": [{"recipient_party_id": payee, "share_bp": 10000}]}


def oblid(oid="cofund", due="2028-12-31", assignee="USCO", required=True):
    return {"obligation_id": oid, "kind": "co_dev", "description": "共同开发",
            "assignee_party_id": assignee, "due_date": due, "required": required}


def version(effective="2026-11-01", end="2031-12-31", *, scopes=None, sub=None,
            obligations=None, terms=None, currency="USD"):
    return {
        "effective_date": effective, "term_end_date": end, "currency": currency,
        "scopes": scopes or [scope()],
        "sublicense": sub or {"scope": "any"},
        "obligations": obligations or [],
        "terms": terms or [upfront()],
    }


# --------------------------------------------------------------------- 纯函数

class DomainTests(unittest.TestCase):
    def test_scope_row_semantics(self):
        wide = Scope.from_dict(scope(regions=("WORLD",), excludes=("CN",)))
        self.assertTrue(wide.covers_region("EU"))
        self.assertFalse(wide.covers_region("CN"))
        row = Scope.from_dict(scope(regions=("ROW",)))
        self.assertTrue(row.covers_region("EU"))

    def test_overlap_requires_all_dimensions(self):
        eu = Scope.from_dict(scope(regions=("EU",), indications=("ONC",)))
        jp = Scope.from_dict(scope(regions=("JP",), indications=("ONC",)))
        self.assertFalse(eu.overlaps(jp))
        eu_other_ind = Scope.from_dict(scope(regions=("EU",), indications=("CARD",)))
        self.assertFalse(eu.overlaps(eu_other_ind))
        eu2 = Scope.from_dict(scope(regions=("EU", "UK"), indications=("ONC",)))
        self.assertTrue(eu.overlaps(eu2))

    def test_covers_scope_respects_exclusivity(self):
        non_exclusive = Scope.from_dict(scope(regions=("WORLD",), exclusive=False))
        exclusive = Scope.from_dict(scope(regions=("EU",), exclusive=True))
        self.assertFalse(non_exclusive.covers_scope(exclusive))
        self.assertTrue(Scope.from_dict(scope(regions=("WORLD",), exclusive=True)).covers_scope(exclusive))

    def test_split_amount_covers_every_cent(self):
        from licensing_ops.domain import Share
        parts = split_amount(Decimal("100.00"), [Share("A", 3333), Share("B", 3333), Share("C", 3334)])
        self.assertEqual(sum(parts), Decimal("100.00"))
        self.assertEqual([format(p, "f") for p in parts], ["33.33", "33.33", "33.34"])

    def test_royalty_tiers_marginal(self):
        content = VersionContent.from_dict(version(terms=[royalty(tiers=[
            {"up_to": "100", "rate_percent": "10"},
            {"up_to": None, "rate_percent": "20"},
        ])]))
        term = content.term("roy")
        # 100*10% + 50*20% = 20
        self.assertEqual(term.royalty_amount(Decimal("150")), Decimal("20.00"))

    def test_share_bp_must_total_10000(self):
        bad = version(terms=[{
            "term_id": "t", "kind": "upfront", "label": "x", "amount": "10",
            "due_date": "2026-11-01",
            "shares": [{"recipient_party_id": "A", "share_bp": 9000}],
        }])
        with self.assertRaises(ValueError):
            VersionContent.from_dict(bad)

    def test_version_content_hash_stable(self):
        c1 = VersionContent.from_dict(version())
        c2 = VersionContent.from_dict(version())
        self.assertEqual(c1.content_sha256(), c2.content_sha256())


class ReviewEngineTests(unittest.TestCase):
    def _content(self, **kwargs):
        return VersionContent.from_dict(version(**kwargs))

    def test_exclusivity_conflict_detected(self):
        candidate = self._content(scopes=[scope(regions=("EU",))])
        sibling = VersionContext(
            "OUT-X", "OUT-X:v1", "effective", self._content(scopes=[scope(regions=("EU",))])
        )
        issues = review_version(candidate, "OUT-Y:v1", [sibling], [])
        self.assertTrue(any(i.code == "exclusivity_conflict" for i in issues))

    def test_non_overlapping_regions_pass(self):
        candidate = self._content(scopes=[scope(regions=("JP",))])
        sibling = VersionContext("OUT-X", "OUT-X:v1", "effective", self._content(scopes=[scope(regions=("EU",))]))
        issues = review_version(candidate, "OUT-Y:v1", [sibling], [])
        self.assertEqual(issues, [])

    def test_time_window_prevents_overlap_after_expiry(self):
        candidate = self._content(effective="2032-01-01", end="2035-12-31", scopes=[scope(regions=("EU",))])
        sibling = VersionContext(
            "OUT-X", "OUT-X:v1", "effective",
            self._content(effective="2026-11-01", end="2031-12-31", scopes=[scope(regions=("EU",))]),
        )
        issues = review_version(candidate, "OUT-Y:v1", [sibling], [])
        self.assertEqual([i for i in issues if i.code == "scope_overlap"], [])

    def test_sublicense_exceeds_source_blocks(self):
        candidate = self._content(sub={"scope": "any"})
        source = SourceLink("IN-1", "IN-1:v1", "inbound", self._content(sub={"scope": "none"}))
        issues = review_version(candidate, "OUT-Y:v1", [], [source])
        self.assertTrue(any(i.code == "sublicense_exceeds_source" for i in issues))

    def test_named_sublicense_party_must_be_granted(self):
        candidate = self._content(sub={"scope": "named", "named_parties": ["EUCO", "OTHER"]})
        source = SourceLink("IN-1", "IN-1:v1", "inbound",
                            self._content(sub={"scope": "named", "named_parties": ["EUCO"]}))
        issues = review_version(candidate, "OUT-Y:v1", [], [source])
        self.assertTrue(any(i.code == "sublicense_party_not_granted" for i in issues))

    def test_obligation_gap_when_source_obligation_not_carried(self):
        candidate = self._content(obligations=[])
        source = SourceLink("IN-1", "IN-1:v1", "inbound", self._content(obligations=[oblid("cofund")]))
        issues = review_version(candidate, "OUT-Y:v1", [], [source])
        self.assertTrue(any(i.code == "obligation_gap" for i in issues))
        # 承接或豁免后缺口消失
        waived = review_version(candidate, "OUT-Y:v1", [], [source],
                                waived_obligation_ids=frozenset({"cofund"}))
        self.assertFalse(any(i.code == "obligation_gap" for i in waived))

    def test_source_scope_gap_for_uncovered_region(self):
        candidate = self._content(scopes=[scope(regions=("JP",))])
        source = SourceLink("IN-1", "IN-1:v1", "inbound", self._content(scopes=[scope(regions=("EU",))]))
        issues = review_version(candidate, "OUT-Y:v1", [], [source])
        self.assertTrue(any(i.code == "source_scope_gap" for i in issues))

    def test_renewal_cannot_start_before_prior_term_end(self):
        candidate = self._content(effective="2031-06-01", end="2035-12-31", scopes=[scope(regions=("EU",))])
        issues = review_version(candidate, "OUT-R:v1", [], [],
                                is_renewal=True, previous_term_end="2031-12-31")
        self.assertTrue(any(i.code == "renewal_overlaps_prior_term" for i in issues))
        ok = review_version(self._content(effective="2032-01-01", end="2035-12-31",
                                          scopes=[scope(regions=("EU",))]),
                            "OUT-R:v1", [], [], is_renewal=True, previous_term_end="2031-12-31")
        self.assertFalse(any(i.code == "renewal_overlaps_prior_term" for i in ok))


# --------------------------------------------------------------------- 服务集成

class ServiceTestBase(unittest.TestCase):
    def setUp(self):
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 10, 2, tzinfo=timezone.utc))
        self.svc = LicensingService(self.connection, self.clock)
        for uid, role in (("bd", "bd_manager"), ("deal", "dealmaker"),
                          ("fin", "finance"), ("aud", "auditor")):
            self.svc.create_user("", uid, uid, role)
        self.svc.register_candidate("bd", "X-101", "候选药", "X101")
        self.svc.register_party("bd", "UP", "上游", "licensor")
        self.svc.register_party("bd", "USCO", "我司", "licensee")
        self.svc.register_party("bd", "EUCO", "欧洲", "partner")

    def tearDown(self):
        self.connection.close()

    def _inbound(self, agreement_id="IN-1", sub=None, obligations=None):
        self.svc.create_agreement("bd", {
            "agreement_id": agreement_id, "agreement_no": f"N-{agreement_id}",
            "candidate_id": "X-101", "direction": "inbound",
            "licensor_party_id": "UP", "licensee_party_id": "USCO",
        })
        self.svc.draft_version("bd", agreement_id, version(
            sub=sub or {"scope": "named", "named_parties": ["EUCO"]},
            obligations=obligations if obligations is not None else [oblid()],
            terms=[upfront(), royalty()],
        ))
        self.svc.sign_version("deal", f"{agreement_id}:v1")

    def _outbound_eu(self, agreement_id="OUT-EU", regions=("EU", "UK"), sub=None, obligations=None,
                     shares=None, effective="2027-01-01", end="2031-06-30", tiers=None):
        self.svc.create_agreement("bd", {
            "agreement_id": agreement_id, "agreement_no": f"N-{agreement_id}",
            "candidate_id": "X-101", "direction": "outbound",
            "licensor_party_id": "USCO", "licensee_party_id": "EUCO",
            "parent_agreement_id": "IN-1",
        })
        self.svc.draft_version("bd", agreement_id, version(
            effective=effective, end=end, scopes=[scope(regions=regions)],
            sub=sub or {"scope": "none"},
            obligations=obligations if obligations is not None else [oblid()],
            terms=[royalty(shares=shares, tiers=tiers)],
        ))


class ServiceFlowTests(ServiceTestBase):
    def test_version_chain_and_signing(self):
        self._inbound()
        self._outbound_eu()
        report = self.svc.run_review("bd", "OUT-EU:v1")
        self.assertEqual(report["blocking_count"], 0)
        signed = self.svc.sign_version("deal", "OUT-EU:v1")
        self.assertEqual(signed["state"], "effective")
        versions = self.svc.list_versions("aud", "OUT-EU")
        self.assertEqual(len(versions), 1)

    def test_sign_blocked_on_obligation_gap(self):
        self._inbound(obligations=[oblid("cofund")])
        self._outbound_eu(obligations=[])
        with self.assertRaises(ReviewBlocked):
            self.svc.sign_version("deal", "OUT-EU:v1")
        # 阻断评审报告必须落库留痕，不随拒签回滚。
        reviews = self.connection.execute(
            "SELECT blocking_count FROM version_reviews WHERE version_id='OUT-EU:v1'"
        ).fetchall()
        self.assertTrue(any(r["blocking_count"] > 0 for r in reviews))

    def test_dispute_skips_already_settled_share(self):
        self._inbound()
        shares = [{"recipient_party_id": "USCO", "share_bp": 8000},
                  {"recipient_party_id": "UP", "share_bp": 2000}]
        self._outbound_eu(shares=shares)
        self.svc.sign_version("deal", "OUT-EU:v1")
        event = self.svc.record_event("fin", {
            "event_id": "EV-1", "agreement_id": "OUT-EU", "kind": "royalty",
            "term_id": "roy", "period": "2027", "region": "EU", "indication": "ONC",
            "face_amount": "1000", "event_date": "2027-06-01",
        })
        us_dist = next(d for d in event["distributions"] if d["recipient_party_id"] == "USCO")
        up_dist = next(d for d in event["distributions"] if d["recipient_party_id"] == "UP")
        self.svc.confirm_distribution("fin", us_dist["distribution_id"])
        self.svc.settle_distribution("fin", us_dist["distribution_id"], "已结算")
        # 对两方提争议：已结算的 USCO 份额被跳过，仅冻结 UP，事件为部分冻结。
        dispute = self.svc.open_dispute("fin", "EV-1", "异议", ["USCO", "UP"])
        self.assertEqual(dispute["frozen_recipients"], ["UP"])
        self.assertEqual(self.svc.event("fin", "EV-1")["state"], "frozen_partial")
        self.assertEqual(
            self.svc.distribution("fin", us_dist["distribution_id"])["state"], "confirmed")
        self.assertEqual(
            self.svc.distribution("fin", up_dist["distribution_id"])["state"], "frozen")

    def test_role_permission_enforced(self):
        with self.assertRaises(Forbidden):
            self.svc.register_candidate("aud", "Y", "y")
        self._inbound()
        # bd_manager 不能签署
        self._outbound_eu()
        with self.assertRaises(Forbidden):
            self.svc.sign_version("bd", "OUT-EU:v1")

    def test_event_references_version_in_force_on_event_date(self):
        self._inbound()
        shares = [{"recipient_party_id": "USCO", "share_bp": 8000},
                  {"recipient_party_id": "UP", "share_bp": 2000}]
        self._outbound_eu(shares=shares)
        self.svc.sign_version("deal", "OUT-EU:v1")
        # v2 于 2028 生效
        self.svc.draft_version("bd", "OUT-EU", version(
            effective="2028-01-01", end="2031-06-30", scopes=[scope(regions=("EU", "UK"))],
            sub={"scope": "none"}, obligations=[oblid()],
            terms=[royalty(tiers=[{"up_to": None, "rate_percent": "20"}], shares=shares)],
        ))
        self.svc.sign_version("deal", "OUT-EU:v2")
        event = self.svc.record_event("fin", {
            "event_id": "EV-OLD", "agreement_id": "OUT-EU", "kind": "royalty",
            "term_id": "roy", "period": "2027", "region": "EU", "indication": "ONC",
            "face_amount": "1000", "event_date": "2027-06-01",
        })
        self.assertEqual(event["version_id"], "OUT-EU:v1")
        event2 = self.svc.record_event("fin", {
            "event_id": "EV-NEW", "agreement_id": "OUT-EU", "kind": "royalty",
            "term_id": "roy", "period": "2028", "region": "EU", "indication": "ONC",
            "face_amount": "1000", "event_date": "2028-06-01",
        })
        self.assertEqual(event2["version_id"], "OUT-EU:v2")

    def test_settled_amount_cannot_be_reversed_or_changed(self):
        self._inbound()
        shares = [{"recipient_party_id": "USCO", "share_bp": 10000}]
        self._outbound_eu(shares=shares)
        self.svc.sign_version("deal", "OUT-EU:v1")
        event = self.svc.record_event("fin", {
            "event_id": "EV-1", "agreement_id": "OUT-EU", "kind": "royalty",
            "term_id": "roy", "period": "2027", "region": "EU", "indication": "ONC",
            "face_amount": "500", "event_date": "2027-06-01",
        })
        dist_id = event["distributions"][0]["distribution_id"]
        self.svc.confirm_distribution("fin", dist_id)
        self.svc.settle_distribution("fin", dist_id, "结算")
        with self.assertRaises(InvalidState):
            self.svc.reverse_distribution("fin", dist_id, "冲正")

    def test_dispute_freezes_only_named_share(self):
        self._inbound()
        shares = [{"recipient_party_id": "USCO", "share_bp": 8000},
                  {"recipient_party_id": "UP", "share_bp": 2000}]
        self._outbound_eu(shares=shares)
        self.svc.sign_version("deal", "OUT-EU:v1")
        event = self.svc.record_event("fin", {
            "event_id": "EV-1", "agreement_id": "OUT-EU", "kind": "royalty",
            "term_id": "roy", "period": "2027", "region": "EU", "indication": "ONC",
            "face_amount": "1000", "event_date": "2027-06-01",
        })
        up_dist = next(d for d in event["distributions"] if d["recipient_party_id"] == "UP")
        us_dist = next(d for d in event["distributions"] if d["recipient_party_id"] == "USCO")
        self.svc.open_dispute("fin", "EV-1", "异议", ["UP"])
        self.assertEqual(self.svc.event("fin", "EV-1")["state"], "frozen_partial")
        # 未涉争议份额照常确认/结算
        self.svc.confirm_distribution("fin", us_dist["distribution_id"])
        self.svc.settle_distribution("fin", us_dist["distribution_id"], "ok")
        # 冻结份额不能确认
        with self.assertRaises(InvalidState):
            self.svc.confirm_distribution("fin", up_dist["distribution_id"])
        # 争议解决后份额解冻回 pending
        dispute_id = self.connection.execute(
            "SELECT dispute_id FROM disputes WHERE event_id='EV-1' AND state='open'"
        ).fetchone()["dispute_id"]
        self.svc.resolve_dispute("fin", dispute_id, "解决")
        self.svc.confirm_distribution("fin", up_dist["distribution_id"])

    def test_upfront_instantiated_on_signing(self):
        self._inbound()
        # IN-1 的 upfront 条款在签署时生成应收事件与待确认分配。
        event = self.svc.event("fin", "EV-IN-1:v1-upfront")
        self.assertEqual(event["kind"], "upfront")
        self.assertEqual(event["version_id"], "IN-1:v1")
        self.assertEqual(event["face_amount"], "1000.00")
        self.assertTrue(event["distributions"])
        self.assertEqual(event["distributions"][0]["state"], "pending")

    def test_forecast_is_replayable_and_then_confirmed(self):
        self._inbound()
        self._outbound_eu(shares=[{"recipient_party_id": "USCO", "share_bp": 10000}])
        self.svc.sign_version("deal", "OUT-EU:v1")
        self.svc.create_assumptions("fin", "ASM", {
            "currency": "USD", "horizon_end": "2030-12-31",
            "sales": [{"period": "2027", "region": "EU", "indication": "ONC", "net_sales": "100"}],
            "milestones": [],
        })
        first = self.svc.forecast("fin", "ASM", "2027-01-01")
        second = self.svc.forecast("fin", "ASM", "2027-01-01")
        self.assertEqual(first["forecast_id"], second["forecast_id"])
        self.assertTrue(second["replayed"])

    def test_renewal_cannot_extend_prior_rights(self):
        self._inbound()
        self._outbound_eu()
        self.svc.sign_version("deal", "OUT-EU:v1")
        self.svc.create_agreement("bd", {
            "agreement_id": "OUT-R", "agreement_no": "N-R", "candidate_id": "X-101",
            "direction": "outbound", "licensor_party_id": "USCO", "licensee_party_id": "EUCO",
            "parent_agreement_id": "IN-1", "renewal_of_agreement_id": "OUT-EU",
        })
        self.svc.draft_version("bd", "OUT-R", version(
            effective="2031-01-01", end="2035-12-31", scopes=[scope(regions=("EU",))],
            sub={"scope": "none"}, obligations=[oblid()], terms=[royalty()],
        ))
        with self.assertRaises(ReviewBlocked):
            self.svc.sign_version("deal", "OUT-R:v1")

    def test_trace_and_chain(self):
        self._inbound()
        self._outbound_eu()
        self.svc.sign_version("deal", "OUT-EU:v1")
        trace = self.svc.trace_region("aud", "EU")
        self.assertTrue(any(g["agreement_id"] == "OUT-EU" for g in trace["grants"]))
        chain = self.svc.authorization_chain("aud", "OUT-EU")
        self.assertEqual(chain["rights_chain_order"], ["OUT-EU", "IN-1"])
        self.assertTrue(self.svc.audit_chain("aud")["valid"])

    def test_obligation_status_tracks_across_versions(self):
        self._inbound()
        self._outbound_eu(obligations=[oblid("cofund")])
        self.svc.sign_version("deal", "OUT-EU:v1")
        unmet = self.svc.unmet_obligations("aud", "X-101")
        self.assertTrue(any(i["obligation_id"] == "cofund" for i in unmet["items"]))
        self.svc.satisfy_obligation("deal", "OUT-EU", "cofund", "已支付")
        unmet_after = self.svc.unmet_obligations("aud", "X-101")
        self.assertFalse(any(i["agreement_id"] == "OUT-EU" for i in unmet_after["items"]))

    def test_event_idempotent(self):
        self._inbound()
        self._outbound_eu(shares=[{"recipient_party_id": "USCO", "share_bp": 10000}])
        self.svc.sign_version("deal", "OUT-EU:v1")
        payload = {
            "event_id": "EV-1", "agreement_id": "OUT-EU", "kind": "royalty",
            "term_id": "roy", "period": "2027", "region": "EU", "indication": "ONC",
            "face_amount": "1000", "event_date": "2027-06-01", "idempotency_key": "K1",
        }
        first = self.svc.record_event("fin", payload)
        second = self.svc.record_event("fin", payload)
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["event_id"], second["event_id"])


# --------------------------------------------------------------------- HTTP API

class ApiTests(ServiceTestBase):
    def setUp(self):
        super().setUp()
        self.app = LedgerApplication(self.svc)

    def _call(self, method, path, actor="bd", body=None):
        data = json.dumps(body or {}).encode()
        return self.app.handle(method, path, {"X-Actor-Id": actor, "Content-Length": str(len(data))}, data)

    def test_health(self):
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)

    def test_requires_actor_header(self):
        response = self.app.handle("GET", "/ledger")
        self.assertEqual(response.status, 422)

    def test_full_path_via_http(self):
        self._call("POST", "/candidates", body={"candidate_id": "C2", "name": "n"})
        r = self._call("GET", "/candidates/C2/trace", actor="aud")
        self.assertEqual(r.status, 200)
        self.assertEqual(r.body["candidate_id"], "C2")

    def test_forbidden_maps_to_403(self):
        response = self._call("POST", "/candidates", actor="aud", body={"candidate_id": "C3", "name": "n"})
        self.assertEqual(response.status, 403)

    def test_unknown_route_404(self):
        response = self._call("GET", "/nope")
        self.assertEqual(response.status, 404)


if __name__ == "__main__":
    unittest.main()
