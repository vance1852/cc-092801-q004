"""SQLite 结构、事务和审计事件辅助函数。"""
from __future__ import annotations
import json, sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(user_id TEXT PRIMARY KEY,role TEXT NOT NULL,salt TEXT NOT NULL,password_hash TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY,user_id TEXT NOT NULL,expires_at TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS zone_records(zone_record_id TEXT PRIMARY KEY,collection_zone TEXT NOT NULL,biosafety_type TEXT NOT NULL,length_m REAL NOT NULL,criticality INTEGER NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS monitoring_records(monitoring_record_id TEXT PRIMARY KEY,zone_record_id TEXT NOT NULL REFERENCES zone_records(zone_record_id),sensor_source_id TEXT NOT NULL,speed_kmh REAL NOT NULL,traffic_flow_vph REAL NOT NULL,impact_index REAL NOT NULL,observed_at TEXT NOT NULL,UNIQUE(zone_record_id,sensor_source_id,observed_at));
CREATE TABLE IF NOT EXISTS alerts(alert_id TEXT PRIMARY KEY,zone_record_id TEXT NOT NULL REFERENCES zone_records(zone_record_id),fingerprint TEXT NOT NULL UNIQUE,severity TEXT NOT NULL,score REAL NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,resolved_at TEXT);
CREATE TABLE IF NOT EXISTS treatment_tickets(treatment_ticket_id TEXT PRIMARY KEY,zone_record_id TEXT NOT NULL,alert_id TEXT NOT NULL,assignee TEXT NOT NULL,status TEXT NOT NULL,priority INTEGER NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS preservation_resources(preservation_resource_id TEXT PRIMARY KEY,kind TEXT NOT NULL,collection_zone TEXT NOT NULL,capacity INTEGER NOT NULL,available INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS allocations(plan_id TEXT PRIMARY KEY,preservation_resource_id TEXT NOT NULL,treatment_ticket_id TEXT NOT NULL,quantity INTEGER NOT NULL,created_at TEXT NOT NULL,UNIQUE(preservation_resource_id,treatment_ticket_id));
CREATE TABLE IF NOT EXISTS audit_events(event_id INTEGER PRIMARY KEY AUTOINCREMENT,entity_type TEXT NOT NULL,entity_id TEXT NOT NULL,action TEXT NOT NULL,actor TEXT NOT NULL,payload TEXT NOT NULL,created_at TEXT NOT NULL);

-- ============================================================
-- 版本化海外授权与收益台账（append-only；已结算行不可被新版本改写）
-- ============================================================
CREATE TABLE IF NOT EXISTS lic_users(
  user_id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('bd_manager','dealmaker','finance','auditor')),
  active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS candidates(
  candidate_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  internal_code TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS counterparties(
  party_id TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('licensor','licensee','partner')),
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS agreements(
  agreement_id TEXT PRIMARY KEY,
  agreement_no TEXT NOT NULL UNIQUE,
  candidate_id TEXT NOT NULL REFERENCES candidates(candidate_id),
  direction TEXT NOT NULL CHECK(direction IN ('inbound','outbound')),
  licensor_party_id TEXT NOT NULL REFERENCES counterparties(party_id),
  licensee_party_id TEXT NOT NULL REFERENCES counterparties(party_id),
  parent_agreement_id TEXT REFERENCES agreements(agreement_id),
  renewal_of_agreement_id TEXT REFERENCES agreements(agreement_id),
  state TEXT NOT NULL DEFAULT 'draft' CHECK(state IN ('draft','under_review','active','expired','terminated')),
  auto_renew INTEGER NOT NULL DEFAULT 0 CHECK(auto_renew IN (0,1)),
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  CHECK(licensor_party_id <> licensee_party_id)
);
CREATE TABLE IF NOT EXISTS agreement_versions(
  version_id TEXT PRIMARY KEY,
  agreement_id TEXT NOT NULL REFERENCES agreements(agreement_id),
  seq INTEGER NOT NULL,
  parent_version_id TEXT REFERENCES agreement_versions(version_id),
  effective_date TEXT NOT NULL,
  term_end_date TEXT,
  currency TEXT NOT NULL,
  content_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'proposed' CHECK(state IN ('proposed','effective','superseded','discarded')),
  superseded_by_version_id TEXT REFERENCES agreement_versions(version_id),
  signed_at TEXT,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(agreement_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_versions_effective
ON agreement_versions(agreement_id, state, effective_date);
CREATE TABLE IF NOT EXISTS version_reviews(
  review_id INTEGER PRIMARY KEY AUTOINCREMENT,
  agreement_id TEXT NOT NULL,
  version_id TEXT NOT NULL REFERENCES agreement_versions(version_id),
  blocking_count INTEGER NOT NULL,
  report_json TEXT NOT NULL,
  reviewed_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_version_reviews ON version_reviews(version_id, review_id);
-- 义务跨版本按 (agreement_id, obligation_id) 跟踪：新版本引用同编号义务时沿用既有履行状态。
CREATE TABLE IF NOT EXISTS obligation_status(
  agreement_id TEXT NOT NULL,
  obligation_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','satisfied','waived')),
  evidence_note TEXT,
  updated_by TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(agreement_id, obligation_id)
);
CREATE TABLE IF NOT EXISTS assumptions(
  assumptions_id TEXT PRIMARY KEY,
  currency TEXT NOT NULL,
  horizon_end TEXT NOT NULL,
  content_json TEXT NOT NULL,
  content_sha256 TEXT NOT NULL UNIQUE,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS forecasts(
  forecast_id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  assumptions_id TEXT NOT NULL REFERENCES assumptions(assumptions_id),
  assumptions_sha256 TEXT NOT NULL,
  basis_json TEXT NOT NULL,
  input_sha256 TEXT NOT NULL UNIQUE,
  result_json TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS disputes(
  dispute_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  frozen_recipients_json TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','resolved')),
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  resolved_at TEXT,
  resolution_note TEXT
);
CREATE TABLE IF NOT EXISTS revenue_events(
  event_id TEXT PRIMARY KEY,
  agreement_id TEXT NOT NULL REFERENCES agreements(agreement_id),
  version_id TEXT NOT NULL REFERENCES agreement_versions(version_id),
  term_id TEXT NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('upfront','milestone','royalty')),
  period TEXT NOT NULL,
  region TEXT,
  indication TEXT,
  face_amount TEXT NOT NULL,
  currency TEXT NOT NULL,
  event_date TEXT NOT NULL,
  source_note TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'recorded' CHECK(state IN ('recorded','frozen_partial','frozen_full')),
  dispute_id TEXT REFERENCES disputes(dispute_id),
  idempotency_key TEXT NOT NULL UNIQUE,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_agreement_period
ON revenue_events(agreement_id, period);
CREATE TABLE IF NOT EXISTS distributions(
  distribution_id TEXT PRIMARY KEY,
  event_id TEXT NOT NULL REFERENCES revenue_events(event_id),
  agreement_id TEXT NOT NULL,
  version_id TEXT NOT NULL,
  term_id TEXT NOT NULL,
  period TEXT NOT NULL,
  recipient_party_id TEXT NOT NULL,
  share_bp INTEGER NOT NULL,
  amount TEXT NOT NULL,
  currency TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','confirmed','frozen','reversed')),
  dispute_id TEXT REFERENCES disputes(dispute_id),
  confirmed_at TEXT,
  reversed_at TEXT,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_distributions_recipient_period
ON distributions(recipient_party_id, period, state);
-- 结算流水只追加；存在结算行的分配即被锁定，任何新版本或冲正都不得改动金额。
CREATE TABLE IF NOT EXISTS settlements(
  settlement_id INTEGER PRIMARY KEY AUTOINCREMENT,
  distribution_id TEXT NOT NULL REFERENCES distributions(distribution_id),
  amount TEXT NOT NULL,
  currency TEXT NOT NULL,
  period TEXT NOT NULL,
  note TEXT NOT NULL,
  created_by TEXT NOT NULL,
  settled_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_settlements_distribution ON settlements(distribution_id);
CREATE TABLE IF NOT EXISTS lic_audit_events(
  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  previous_hash TEXT NOT NULL,
  event_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_lic_audit_entity
ON lic_audit_events(entity_type, entity_id, event_id);
"""
def utcnow() -> str: return datetime.now(timezone.utc).isoformat()
def initialize(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
def connect(path: str = ":memory:", *, check_same_thread: bool = True) -> sqlite3.Connection:
    db=sqlite3.connect(path,timeout=10,check_same_thread=check_same_thread); db.row_factory=sqlite3.Row; db.execute("PRAGMA foreign_keys=ON"); db.execute("PRAGMA journal_mode=WAL"); initialize(db); db.commit(); return db
@contextmanager
def transaction(db: sqlite3.Connection, *, immediate: bool = False) -> Iterator[sqlite3.Connection]:
    # 既有实现默认即以 BEGIN IMMEDIATE 开事务；immediate 参数用于与台账服务调用风格保持一致。
    try: db.execute("BEGIN IMMEDIATE"); yield db; db.commit()
    except Exception: db.rollback(); raise
def audit(db, entity_type, entity_id, action, actor, payload):
    db.execute("INSERT INTO audit_events(entity_type,entity_id,action,actor,payload,created_at) VALUES(?,?,?,?,?,?)",(entity_type,entity_id,action,actor,json.dumps(payload,ensure_ascii=False,sort_keys=True),utcnow()))
def rows(db, query, args=()): return [dict(r) for r in db.execute(query,args).fetchall()]
