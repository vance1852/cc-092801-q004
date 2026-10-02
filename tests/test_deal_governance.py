from __future__ import annotations

import sqlite3
import unittest
from datetime import date
from decimal import Decimal

from deal_governance.clock import FrozenClock
from deal_governance.economics import build_projection_lines, waterfall_allocations
from deal_governance.errors import (
    Conflict,
    Forbidden,
    InvalidState,
    NotFound,
    SigningBlocked,
    ValidationFailed,
)
from deal_governance.models import Scope, TermRevision
from deal_governance.service import DealGovernanceService


def terms(**overrides):
    scope_overrides = {}
    for key in ("territories", "indications", "stages"):
        if key in overrides:
            scope_overrides[key] = overrides.pop(key)
    raw = {
        "effective_date": "2026-02-01",
        "expiry_date": "2030-12-31",
        "licensor_party_id": "P1",
        "licensee_party_id": "P2",
        "scope": {"territories": ["EU"], "indications": ["ONC"], "stages": ["phase3", "approved"]},
        "exclusivity": "exclusive",
        "sublicense_scope": "same-scope",
        "sublicense_income_share": "0.25",
        "upfront_amount": "10000000",
        "milestones": [{
            "milestone_id": "m1", "milestone_type": "development",
            "name": "III 期启动", "amount": "20000000",
            "trigger_event": "phase3 fpi",
        }],
        "royalty": {"rate": "0.12"},
        "obligations": [
            {"obligation_id": "d1", "kind": "development",
             "description": "推进 III 期", "due_date": "2027-12-31",
             "due_event": None, "owner_party_id": "P2"},
            {"obligation_id": "c1", "kind": "commercial",
             "description": "上市", "due_date": None,
             "due_event": "approval+12m", "owner_party_id": "P2"},
        ],
        "change_note": "初始版本",
    }
    raw.update(overrides)
    raw["scope"].update(scope_overrides)
    return raw


class DealGovernanceTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(date(2026, 2, 1))
        self.service = DealGovernanceService(self.connection, self.clock)
        for uid, role in (("bd", "bd"), ("mgr", "manager"),
                          ("fin", "finance"), ("aud", "auditor")):
            self.service.create_user(None, uid, uid, role)
        self.service.register_candidate("bd", {"candidate_id": "C1", "name": "X-01"})
        for pid, name in (("P1", "授权方"), ("P2", "欧洲方"),
                          ("P3", "日本方"), ("P4", "第三方")):
            self.service.register_party("bd", {"party_id": pid, "name": name})
        self.service.create_agreement("bd", "A1", "C1", "主授权")

    def tearDown(self) -> None:
        self.connection.close()

    def draft_and_sign(self, agreement_id="A1", actor="mgr", **overrides):
        self.service.draft_revision("bd", agreement_id, terms(**overrides))
        rev_no = self.connection.execute(
            "SELECT MAX(revision_no) AS r FROM term_revisions WHERE agreement_id=?",
            (agreement_id,),
        ).fetchone()["r"]
        revision_id = f"{agreement_id}-r{rev_no}"
        self.service.sign_revision(actor, revision_id)
        return revision_id


class ModelTests(unittest.TestCase):
    def test_scope_overlap_and_contains(self):
        a = Scope(("EU", "JP"), ("ONC",), ("phase3", "approved"))
        b = Scope(("JP",), ("ONC",), ("approved",))
        c = Scope(("US",), ("ONC",), ("approved",))
        self.assertTrue(a.overlaps(b))
        self.assertTrue(a.contains(b))
        self.assertFalse(a.overlaps(c))
        self.assertFalse(a.contains(c))

    def test_term_revision_rejects_bad_inputs(self):
        with self.assertRaises(ValueError):
            TermRevision.from_dict(terms(exclusivity="weird"), 1)
        with self.assertRaises(ValueError):
            TermRevision.from_dict(terms(royalty={"rate": "1.5"}), 1)
        with self.assertRaises(ValueError):
            TermRevision.from_dict(terms(
                effective_date="2030-01-01", expiry_date="2029-01-01"), 1)
        with self.assertRaises(ValueError):
            TermRevision.from_dict(terms(
                obligations=[{"obligation_id": "x", "kind": "reporting",
                              "description": "无期限", "due_date": None,
                              "due_event": None, "owner_party_id": "P2"}]), 1)


class RevisionAndSigningTests(DealGovernanceTestBase):
    def test_draft_sign_supersede_cycle(self):
        r1 = self.draft_and_sign(upfront_amount="10000000")
        self.clock.advance(days=400)
        self.service.draft_revision("bd", "A1",
                                    terms(change_note="修订", effective_date="2027-03-01"))
        self.service.sign_revision("mgr", "A1-r2")
        self.assertEqual(self.service.get_revision("aud", r1)["state"], "superseded")
        self.assertEqual(self.service.get_revision("aud", "A1-r2")["state"], "effective")
        # 旧版本快照不可变
        row = self.connection.execute(
            "SELECT upfront_amount FROM term_revisions WHERE revision_id=?", (r1,)
        ).fetchone()
        self.assertEqual(row["upfront_amount"], "10000000.00")

    def test_exclusivity_conflict_blocks_signing(self):
        self.draft_and_sign(territories=["JP"])
        self.service.create_agreement("bd", "A2", "C1", "冲突授权")
        self.service.draft_revision("bd", "A2", terms(
            licensee_party_id="P4", territories=["JP"], stages=["approved"],
            sublicense_scope="none", sublicense_income_share="0",
            milestones=[], royalty=None,
            obligations=[{"obligation_id": "c1", "kind": "commercial",
                          "description": "上市", "due_date": None,
                          "due_event": "approval+12m", "owner_party_id": "P4"}]))
        findings = self.service.pre_sign_findings("mgr", "A2-r1")
        self.assertTrue(any(f["code"] == "exclusivity_conflict" for f in findings))
        with self.assertRaises(SigningBlocked):
            self.service.sign_revision("mgr", "A2-r1")

    def test_non_exclusive_overlap_is_warning_not_blocker(self):
        self.draft_and_sign(territories=["JP"], exclusivity="non-exclusive",
                            sublicense_scope="none", sublicense_income_share="0")
        self.service.create_agreement("bd", "A2", "C1", "并行非排他")
        self.service.draft_revision("bd", "A2", terms(
            licensee_party_id="P4", territories=["JP"], stages=["approved"],
            exclusivity="non-exclusive", sublicense_scope="none",
            sublicense_income_share="0", milestones=[]))
        findings = self.service.pre_sign_findings("mgr", "A2-r1")
        self.assertTrue(findings)
        self.assertFalse(any(f["severity"] == "blocker" for f in findings))

    def test_obligation_gap_blocks_clinical_scope(self):
        with self.assertRaises(SigningBlocked):
            self.draft_and_sign(
                stages=["phase2"],
                milestones=[],
                obligations=[{"obligation_id": "r", "kind": "reporting",
                              "description": "报告", "due_date": "2027-01-01",
                              "due_event": None, "owner_party_id": "P2"}])

    def test_downstream_territory_overlap_is_not_conflict(self):
        # 源头授权覆盖 JP，下游再许可 JP 不应与上游冲突
        self.draft_and_sign(territories=["EU", "JP"])
        self.service.create_agreement("bd", "A2", "C1", "日本再许可")
        self.service.draft_revision("bd", "A2", terms(
            licensor_party_id="P2", licensee_party_id="P3",
            territories=["JP"], stages=["approved"], exclusivity="non-exclusive",
            sublicense_scope="none", sublicense_income_share="0",
            upfront_amount="5000000", milestones=[], royalty={"rate": "0.08"},
            source_revision_id="A1-r1",
            obligations=[{"obligation_id": "c1", "kind": "commercial",
                          "description": "上市", "due_date": None,
                          "due_event": "approval+6m", "owner_party_id": "P3"}]))
        self.assertEqual(self.service.pre_sign_findings("mgr", "A2-r1"), [])
        self.service.sign_revision("mgr", "A2-r1")

    def test_role_separation(self):
        self.service.draft_revision("bd", "A1", terms())
        with self.assertRaises(Forbidden):
            self.service.sign_revision("aud", "A1-r1")  # 审计无权签署
        with self.assertRaises(Forbidden):
            self.service.record_payment_event("bd", {})  # BD 无权确认收款


class SourceRevisionTests(DealGovernanceTestBase):
    def test_sublicense_requires_effective_source(self):
        # 来源尚为草稿不能引用
        self.service.draft_revision("bd", "A1", terms())
        self.service.create_agreement("bd", "A2", "C1", "再许可")
        with self.assertRaises(InvalidState):
            self.service.draft_revision("bd", "A2", terms(
                licensor_party_id="P2", licensee_party_id="P3",
                sublicense_scope="none", sublicense_income_share="0",
                milestones=[], source_revision_id="A1-r1"))

    def test_sublicense_scope_cannot_exceed_source(self):
        self.draft_and_sign(territories=["EU"])
        self.service.create_agreement("bd", "A2", "C1", "越权")
        with self.assertRaises(ValidationFailed):
            self.service.draft_revision("bd", "A2", terms(
                licensor_party_id="P2", licensee_party_id="P3",
                territories=["US"], sublicense_scope="none",
                sublicense_income_share="0", milestones=[],
                source_revision_id="A1-r1"))

    def test_no_sublicense_rights_rejects_downstream(self):
        self.draft_and_sign(sublicense_scope="none", sublicense_income_share="0")
        self.service.create_agreement("bd", "A2", "C1", "无再许可权")
        with self.assertRaises(ValidationFailed):
            self.service.draft_revision("bd", "A2", terms(
                licensor_party_id="P2", licensee_party_id="P3",
                sublicense_scope="none", sublicense_income_share="0",
                milestones=[], source_revision_id="A1-r1"))

    def test_downstream_term_cannot_extend_beyond_source_expiry(self):
        self.draft_and_sign(expiry_date="2028-12-31")
        self.service.create_agreement("bd", "A2", "C1", "超期再许可")
        with self.assertRaises(ValidationFailed):
            self.service.draft_revision("bd", "A2", terms(
                licensor_party_id="P2", licensee_party_id="P3",
                expiry_date="2031-12-31", sublicense_scope="none",
                sublicense_income_share="0", milestones=[],
                source_revision_id="A1-r1"))


class ProjectionTests(DealGovernanceTestBase):
    def test_projection_is_recomputable(self):
        self.draft_and_sign()
        assumptions = {"milestone_probabilities": {"m1": "0.5"},
                       "annual_net_sales": {"2029": "100000000"}}
        p1 = self.service.create_projection("fin", "A1-r1", assumptions)
        p2 = self.service.create_projection("fin", "A1-r1", assumptions)
        self.assertEqual(p1["lines_hash"], p2["lines_hash"])
        # 10,000,000 首付 + 20,000,000×0.5 里程碑 + 100,000,000×0.12 分成
        self.assertEqual(p1["expected_total"], "32000000.00")

    def test_missing_probability_assumption_rejected(self):
        self.draft_and_sign()
        with self.assertRaises(ValueError):
            build_projection_lines(
                TermRevision.from_dict(terms(), 1),
                {"milestone_probabilities": {}, "annual_net_sales": {}})

    def test_projection_binds_snapshot_hash(self):
        r1 = self.draft_and_sign(royalty={"rate": "0.10"})
        p = self.service.create_projection("fin", r1, {
            "milestone_probabilities": {"m1": "1"},
            "annual_net_sales": {"2029": "100000000"}})
        revision = self.service.get_revision("aud", r1)
        self.assertEqual(p["snapshot_hash"], revision["snapshot_hash"])


class PaymentAndDisputeTests(DealGovernanceTestBase):
    def _two_tier(self):
        self.draft_and_sign(territories=["EU", "JP"])
        self.service.create_agreement("bd", "A2", "C1", "日本再许可")
        self.service.draft_revision("bd", "A2", terms(
            licensor_party_id="P2", licensee_party_id="P3",
            territories=["JP"], stages=["approved"], exclusivity="non-exclusive",
            sublicense_scope="none", sublicense_income_share="0",
            upfront_amount="5000000", milestones=[], royalty={"rate": "0.08"},
            source_revision_id="A1-r1",
            obligations=[{"obligation_id": "c1", "kind": "commercial",
                          "description": "上市", "due_date": None,
                          "due_event": "approval+6m", "owner_party_id": "P3"}]))
        self.service.sign_revision("mgr", "A2-r1")

    def test_idempotent_event(self):
        self.draft_and_sign()
        payload = {"revision_id": "A1-r1", "payment_kind": "upfront",
                   "event_date": "2026-02-05", "gross_amount": "10000000",
                   "currency": "USD", "idempotency_key": "k1"}
        first = self.service.record_payment_event("fin", payload)
        second = self.service.record_payment_event("fin", payload)
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(
            self.connection.execute("SELECT count(*) FROM payment_events").fetchone()[0], 1)

    def test_sublicense_upfront_waterfall(self):
        self._two_tier()
        event = self.service.record_payment_event("fin", {
            "revision_id": "A2-r1", "payment_kind": "sublicense",
            "event_date": "2026-03-01", "gross_amount": "5000000",
            "idempotency_key": "sub1"})
        roles = {a["flow_role"]: a for a in event["allocations"]}
        self.assertEqual(roles["retained"]["amount"], "3750000.00")
        self.assertEqual(roles["retained"]["state"], "settled")  # 留存仅记账
        self.assertEqual(roles["passthrough"]["amount"], "1250000.00")
        self.assertEqual(roles["passthrough"]["recipient_party_id"], "P1")
        self.assertEqual(roles["passthrough"]["basis"]["source_revision_id"], "A1-r1")

    def test_royalty_waterfall_uses_each_snapshot_rate(self):
        self._two_tier()
        event = self.service.record_payment_event("fin", {
            "revision_id": "A2-r1", "payment_kind": "royalty",
            "event_date": "2029-06-30", "period_label": "2029H1",
            "basis_amount": "50000000", "idempotency_key": "roy1"})
        roles = {a["flow_role"]: a for a in event["allocations"]}
        self.assertEqual(roles["direct"]["amount"], "4000000.00")   # 8%
        self.assertEqual(roles["passthrough"]["amount"], "6000000.00")  # 12%
        self.assertEqual(roles["passthrough"]["basis"]["formula"], "50000000.00 × 0.1200")

    def test_dispute_freezes_only_linked_share(self):
        self._two_tier()
        event = self.service.record_payment_event("fin", {
            "revision_id": "A2-r1", "payment_kind": "royalty",
            "event_date": "2029-06-30", "basis_amount": "50000000",
            "idempotency_key": "roy2"})
        passthrough = next(a for a in event["allocations"] if a["flow_role"] == "passthrough")
        direct = next(a for a in event["allocations"] if a["flow_role"] == "direct")
        self.service.open_dispute("fin", event["event_id"],
                                  [passthrough["allocation_id"]], "口径争议")
        # 未冻结份额可照常结算
        self.service.settle_allocation("fin", direct["allocation_id"], "W-1")
        with self.assertRaises(InvalidState):
            self.service.settle_allocation("fin", passthrough["allocation_id"], "W-2")
        # 已结算份额不能再被争议冻结
        with self.assertRaises(InvalidState):
            self.service.open_dispute("fin", event["event_id"],
                                      [direct["allocation_id"]], "试图倒改")

    def test_uphold_voids_and_adjustment_keeps_history(self):
        self._two_tier()
        event = self.service.record_payment_event("fin", {
            "revision_id": "A2-r1", "payment_kind": "sublicense",
            "event_date": "2026-03-01", "gross_amount": "5000000",
            "idempotency_key": "sub2"})
        passthrough = next(a for a in event["allocations"] if a["flow_role"] == "passthrough")
        dispute = self.service.open_dispute("fin", event["event_id"],
                                            [passthrough["allocation_id"]], "金额异议")
        self.service.resolve_dispute("fin", dispute["dispute_id"], "uphold",
                                     adjustments={passthrough["allocation_id"]: "800000"},
                                     note="裁定额 80 万")
        original = self.connection.execute(
            "SELECT state,amount FROM payment_allocations WHERE allocation_id=?",
            (passthrough["allocation_id"],)).fetchone()
        self.assertEqual(original["state"], "voided")
        self.assertEqual(original["amount"], "1250000.00")  # 原行金额不动
        adjustment = self.connection.execute(
            "SELECT amount,state,adjusted_allocation_id FROM payment_allocations"
            " WHERE flow_role='adjustment'").fetchone()
        self.assertEqual(adjustment["amount"], "800000.00")
        self.assertEqual(adjustment["state"], "pending")
        self.assertEqual(adjustment["adjusted_allocation_id"], passthrough["allocation_id"])

    def test_settled_amount_immutable_under_new_revision(self):
        r1 = self.draft_and_sign()
        event = self.service.record_payment_event("fin", {
            "revision_id": r1, "payment_kind": "royalty",
            "event_date": "2029-06-30", "basis_amount": "100000000",
            "idempotency_key": "roy3"})
        alloc_id = event["allocations"][0]["allocation_id"]
        self.service.settle_allocation("fin", alloc_id, "W-9")
        with self.assertRaises(InvalidState):
            self.service.settle_allocation("fin", alloc_id, "W-10")
        # 新版本改变分成比例
        self.clock.advance(days=1500)
        self.service.draft_revision("bd", "A1", terms(
            royalty={"rate": "0.20"}, change_note="重谈",
            effective_date="2030-03-01", milestones=[]))
        self.service.sign_revision("mgr", "A1-r2")
        row = self.connection.execute(
            "SELECT amount,state FROM payment_allocations WHERE allocation_id=?",
            (alloc_id,)).fetchone()
        self.assertEqual(row["amount"], "12000000.00")
        self.assertEqual(row["state"], "settled")

    def test_expired_version_rejects_events_and_renewal_is_explicit(self):
        self.draft_and_sign(expiry_date="2030-12-31")
        self.clock.advance(days=1826)
        with self.assertRaises(InvalidState):
            self.service.record_payment_event("fin", {
                "revision_id": "A1-r1", "payment_kind": "royalty",
                "event_date": "2031-02-01", "basis_amount": "10000000",
                "idempotency_key": "late"})
        # 显式续约新合同后可确认
        self.service.create_agreement("bd", "A2", "C1", "续约")
        self.service.draft_revision("bd", "A2", terms(
            licensee_party_id="P2", territories=["EU"], stages=["approved"],
            sublicense_scope="none", sublicense_income_share="0",
            upfront_amount="0", milestones=[], royalty={"rate": "0.15"},
            effective_date="2031-02-01", expiry_date="2036-12-31"))
        self.service.sign_revision("mgr", "A2-r1")
        event = self.service.record_payment_event("fin", {
            "revision_id": "A2-r1", "payment_kind": "royalty",
            "event_date": "2031-03-01", "basis_amount": "10000000",
            "idempotency_key": "renewed"})
        self.assertEqual(event["allocations"][0]["amount"], "1500000.00")


class TraceTests(DealGovernanceTestBase):
    def test_chain_obligations_and_basis_trace(self):
        self.draft_and_sign(territories=["EU", "JP"])
        self.service.create_agreement("bd", "A2", "C1", "日本再许可")
        self.service.draft_revision("bd", "A2", terms(
            licensor_party_id="P2", licensee_party_id="P3",
            territories=["JP"], stages=["approved"], exclusivity="non-exclusive",
            sublicense_scope="none", sublicense_income_share="0",
            milestones=[], royalty={"rate": "0.08"}, source_revision_id="A1-r1",
            obligations=[{"obligation_id": "c1", "kind": "commercial",
                          "description": "上市", "due_date": "2026-06-01",
                          "due_event": None, "owner_party_id": "P3"}]))
        self.service.sign_revision("mgr", "A2-r1")
        chain = self.service.rights_chain("aud", "A2-r1")
        self.assertEqual([c["revision_id"] for c in chain["chain"]],
                         ["A2-r1", "A1-r1"])
        self.clock.advance(days=365)
        obligations = self.service.open_obligations("aud", candidate_id="C1")
        self.assertGreaterEqual(obligations["overdue_count"], 1)
        overdue = next(o for o in obligations["obligations"] if o["overdue"])
        self.assertEqual(overdue["revision_id"], "A2-r1")
        # 按地区追溯
        jp = self.service.territory_trace("aud", territory="JP")
        self.assertIn("A2-r1", {r["revision_id"] for r in jp})
        eu = self.service.territory_trace("aud", territory="EU")
        self.assertNotIn("A2-r1", {r["revision_id"] for r in eu})
        # 分配依据
        self.service.record_payment_event("fin", {
            "revision_id": "A1-r1", "payment_kind": "upfront",
            "event_date": "2026-02-05", "gross_amount": "10000000",
            "idempotency_key": "k9"})
        basis = self.service.allocation_basis("aud", recipient_party_id="P1")
        self.assertEqual(basis[0]["basis"]["rule"], "direct")
        self.assertEqual(basis[0]["basis"]["revision_id"], "A1-r1")


class EconomicsPureTests(unittest.TestCase):
    def test_upfront_without_ancestors_is_direct(self):
        import json
        term = TermRevision.from_dict(terms(), 1)
        snapshot = {
            "licensor_party_id": term.licensor_party_id,
            "licensee_party_id": term.licensee_party_id,
            "royalty": {"rate": str(term.royalty.rate)} if term.royalty else None,
            "sublicense_income_share": str(term.sublicense_income_share),
        }
        row = {"revision_id": "X-r1", "snapshot_json": json.dumps(snapshot)}
        lines = waterfall_allocations(row, [], "upfront", Decimal("10000000"),
                                      None, "2026-02")
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["flow_role"], "direct")
        self.assertEqual(lines[0]["amount"], "10000000.00")


if __name__ == "__main__":
    unittest.main()
