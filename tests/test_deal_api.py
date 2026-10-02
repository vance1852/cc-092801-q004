from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import date

from deal_governance.api import JsonApplication
from deal_governance.clock import FrozenClock
from deal_governance.service import DealGovernanceService


def post(app, path, payload, actor="bd"):
    return app.handle("POST", path, {"x-actor-id": actor},
                      json.dumps(payload).encode())


class DealApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.service = DealGovernanceService(self.connection, FrozenClock(date(2026, 2, 1)))
        self.app = JsonApplication(self.service)
        self.service.create_user(None, "bd", "商务", "bd")
        self.service.create_user(None, "mgr", "管理", "manager")
        self.service.create_user(None, "fin", "财务", "finance")

    def tearDown(self) -> None:
        self.connection.close()

    def test_health(self):
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["service"], "deal-governance")

    def test_full_flow_over_http(self):
        r = post(self.app, "/candidates", {"candidate_id": "C1", "name": "X-01"})
        self.assertEqual(r.status, 201)
        for pid, name in (("P1", "授权方"), ("P2", "欧洲方")):
            self.assertEqual(post(self.app, "/parties", {"party_id": pid, "name": name}).status, 201)
        self.assertEqual(post(self.app, "/agreements", {
            "agreement_id": "A1", "candidate_id": "C1", "title": "主协议"}).status, 201)

        terms = {
            "effective_date": "2026-02-01", "expiry_date": "2030-12-31",
            "licensor_party_id": "P1", "licensee_party_id": "P2",
            "scope": {"territories": ["EU"], "indications": ["ONC"], "stages": ["approved"]},
            "exclusivity": "exclusive", "sublicense_scope": "none",
            "sublicense_income_share": "0", "upfront_amount": "10000000",
            "milestones": [], "royalty": {"rate": "0.12"},
            "obligations": [{"obligation_id": "c1", "kind": "commercial",
                             "description": "上市", "due_date": None,
                             "due_event": "approval+12m", "owner_party_id": "P2"}],
            "change_note": "v1",
        }
        self.assertEqual(post(self.app, "/agreements/A1/revisions", terms).status, 201)
        pre = post(self.app, "/revisions/A1-r1/pre-sign", {}, "mgr")
        self.assertEqual(pre.status, 200)
        self.assertEqual(pre.body["findings"], [])
        signed = post(self.app, "/revisions/A1-r1/sign", {}, "mgr")
        self.assertEqual(signed.status, 200)
        self.assertEqual(signed.body["state"], "effective")

        event = post(self.app, "/payment-events", {
            "revision_id": "A1-r1", "payment_kind": "royalty",
            "event_date": "2029-06-30", "basis_amount": "100000000",
            "idempotency_key": "k1"}, "fin")
        self.assertEqual(event.status, 201)
        self.assertEqual(event.body["allocations"][0]["amount"], "12000000.00")

    def test_signing_blocked_error_shape(self):
        post(self.app, "/candidates", {"candidate_id": "C1", "name": "X"})
        for pid, name in (("P1", "a"), ("P2", "b"), ("P3", "c")):
            post(self.app, "/parties", {"party_id": pid, "name": name})
        post(self.app, "/agreements", {"agreement_id": "A1", "candidate_id": "C1", "title": "t"})
        post(self.app, "/agreements", {"agreement_id": "A2", "candidate_id": "C1", "title": "t2"})
        terms = {
            "effective_date": "2026-02-01", "expiry_date": "2030-12-31",
            "licensor_party_id": "P1", "licensee_party_id": "P2",
            "scope": {"territories": ["EU"], "indications": ["ONC"], "stages": ["approved"]},
            "exclusivity": "exclusive", "sublicense_scope": "none",
            "sublicense_income_share": "0", "upfront_amount": "1",
            "milestones": [],
            "obligations": [{"obligation_id": "c1", "kind": "commercial",
                             "description": "上市", "due_date": None,
                             "due_event": "x", "owner_party_id": "P2"}],
            "change_note": "v1",
        }
        post(self.app, "/agreements/A1/revisions", terms)
        post(self.app, "/revisions/A1-r1/sign", {}, "mgr")
        terms2 = dict(terms, licensee_party_id="P3", change_note="v2")
        post(self.app, "/agreements/A2/revisions", terms2)
        blocked = post(self.app, "/revisions/A2-r1/sign", {}, "mgr")
        self.assertEqual(blocked.status, 422)
        self.assertEqual(blocked.body["error"]["code"], "signing_blocked")
        self.assertTrue(blocked.body["error"]["findings"])

    def test_missing_actor(self):
        response = self.app.handle("POST", "/candidates", {},
                                   json.dumps({"candidate_id": "C1", "name": "X"}).encode())
        self.assertEqual(response.status, 422)

    def test_forbidden(self):
        response = post(self.app, "/candidates", {"candidate_id": "C1", "name": "X"}, "fin")
        self.assertEqual(response.status, 403)


if __name__ == "__main__":
    unittest.main()
