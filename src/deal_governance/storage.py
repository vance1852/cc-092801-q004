"""授权与收益治理的 SQLite 模式、事务与哈希链审计。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping


def canonical_json(value: Any) -> str:
    """排序键、无空白的 JSON，Decimal 等一律先转字符串，保证可复算。"""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS dg_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('bd','manager','finance','auditor','admin')),
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS candidates (
    candidate_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parties (
    party_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agreements (
    agreement_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
    title TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'negotiating' CHECK(state IN ('negotiating','active','terminated')),
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS term_revisions (
    revision_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL REFERENCES agreements(agreement_id),
    revision_no INTEGER NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('draft','effective','superseded','terminated')),
    effective_date TEXT NOT NULL,
    expiry_date TEXT,
    signed_at TEXT,
    superseded_at TEXT,
    terminated_at TEXT,
    licensor_party_id TEXT NOT NULL,
    licensee_party_id TEXT NOT NULL,
    scope_json TEXT NOT NULL,
    exclusivity TEXT NOT NULL,
    sublicense_scope TEXT NOT NULL,
    upfront_amount TEXT NOT NULL,
    royalty_json TEXT,
    sublicense_income_share TEXT NOT NULL,
    source_revision_id TEXT,
    snapshot_json TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    change_note TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(agreement_id, revision_no)
);

CREATE TABLE IF NOT EXISTS obligations (
    obligation_uid TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES term_revisions(revision_id),
    agreement_id TEXT NOT NULL,
    obligation_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    description TEXT NOT NULL,
    due_date TEXT,
    due_event TEXT,
    owner_party_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','fulfilled','waived')),
    fulfilled_at TEXT,
    fulfilled_by TEXT,
    UNIQUE(revision_id, obligation_id)
);

CREATE TABLE IF NOT EXISTS obligation_fulfillments (
    fulfillment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    obligation_uid TEXT NOT NULL REFERENCES obligations(obligation_uid),
    note TEXT NOT NULL,
    evidence_ref TEXT,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projections (
    projection_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES term_revisions(revision_id),
    as_of_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','committed')),
    assumptions_json TEXT NOT NULL,
    assumptions_hash TEXT NOT NULL,
    snapshot_hash TEXT NOT NULL,
    lines_hash TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    committed_at TEXT
);

CREATE TABLE IF NOT EXISTS projection_lines (
    line_seq INTEGER NOT NULL,
    projection_id TEXT NOT NULL REFERENCES projections(projection_id),
    line_kind TEXT NOT NULL,
    ref_id TEXT NOT NULL,
    period_label TEXT NOT NULL,
    probability TEXT NOT NULL,
    gross_amount TEXT NOT NULL,
    expected_amount TEXT NOT NULL,
    basis TEXT NOT NULL,
    PRIMARY KEY(projection_id, line_seq)
);

CREATE TABLE IF NOT EXISTS payment_events (
    event_id TEXT PRIMARY KEY,
    agreement_id TEXT NOT NULL REFERENCES agreements(agreement_id),
    revision_id TEXT NOT NULL REFERENCES term_revisions(revision_id),
    payment_kind TEXT NOT NULL CHECK(payment_kind IN ('upfront','milestone','royalty','sublicense')),
    period_label TEXT NOT NULL,
    milestone_id TEXT,
    gross_amount TEXT NOT NULL,
    basis_amount TEXT,
    currency TEXT NOT NULL,
    event_date TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payment_allocations (
    allocation_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES payment_events(event_id),
    revision_id TEXT NOT NULL,
    upstream_revision_id TEXT,
    recipient_party_id TEXT NOT NULL,
    flow_role TEXT NOT NULL CHECK(flow_role IN ('direct','retained','passthrough','adjustment')),
    amount TEXT NOT NULL,
    currency TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('pending','held','settled','voided')),
    basis_json TEXT NOT NULL,
    period_label TEXT NOT NULL,
    adjusted_allocation_id TEXT REFERENCES payment_allocations(allocation_id),
    created_at TEXT NOT NULL,
    settled_at TEXT,
    settlement_ref TEXT
);

CREATE INDEX IF NOT EXISTS idx_allocations_event ON payment_allocations(event_id);
CREATE INDEX IF NOT EXISTS idx_allocations_recipient ON payment_allocations(recipient_party_id, state);

CREATE TABLE IF NOT EXISTS disputes (
    dispute_id TEXT PRIMARY KEY,
    event_id TEXT NOT NULL REFERENCES payment_events(event_id),
    reason TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('open','resolved')),
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution_json TEXT
);

CREATE TABLE IF NOT EXISTS dispute_allocations (
    dispute_id TEXT NOT NULL REFERENCES disputes(dispute_id),
    allocation_id TEXT NOT NULL REFERENCES payment_allocations(allocation_id),
    PRIMARY KEY(dispute_id, allocation_id)
);

CREATE TABLE IF NOT EXISTS dg_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def utcnow_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    db = sqlite3.connect(str(path), timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)
    db.commit()
    return db


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def rows(db: sqlite3.Connection, query: str, args: tuple = ()) -> list[dict]:
    return [dict(r) for r in db.execute(query, args).fetchall()]
