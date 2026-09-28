"""打回申诉档案：申诉单、每次处理流水、零件库存缺口的持久化。

本模块只负责档案存取，不含任何判定规则（判定见 appeals.py）。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _j(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


class AppealArchive:
    """申诉相关表的仓库；所有写入均保留留痕，不做物理删除。"""

    SCHEMA = """
    CREATE TABLE IF NOT EXISTS repair_appeals (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      repair_id INTEGER NOT NULL REFERENCES repairs(id),
      recall_id INTEGER NOT NULL REFERENCES recalls(id),
      dealer_id INTEGER NOT NULL REFERENCES dealers(id),
      new_evidence_hash TEXT NOT NULL,
      explanation TEXT NOT NULL,
      status TEXT NOT NULL CHECK(status IN ('pending','upheld','reversed')),
      submitted_by TEXT NOT NULL,
      submitted_at TEXT NOT NULL,
      decided_by TEXT,
      decided_at TEXT,
      decision_note TEXT
    );
    CREATE UNIQUE INDEX IF NOT EXISTS one_pending_appeal_per_repair
      ON repair_appeals(repair_id) WHERE status='pending';
    CREATE TABLE IF NOT EXISTS appeal_events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      appeal_id INTEGER NOT NULL REFERENCES repair_appeals(id),
      repair_id INTEGER NOT NULL REFERENCES repairs(id),
      at TEXT NOT NULL,
      actor TEXT NOT NULL,
      action TEXT NOT NULL,
      details_json TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS part_gaps (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      recall_id INTEGER NOT NULL REFERENCES recalls(id),
      dealer_id INTEGER NOT NULL REFERENCES dealers(id),
      remedy_version INTEGER NOT NULL,
      quantity INTEGER NOT NULL CHECK(quantity>0),
      filled INTEGER NOT NULL DEFAULT 0 CHECK(filled<=quantity),
      appeal_id INTEGER REFERENCES repair_appeals(id),
      created_by TEXT NOT NULL,
      created_at TEXT NOT NULL,
      resolved_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_part_gaps_key ON part_gaps(recall_id,dealer_id,remedy_version);
    """

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ---- 申诉单 ----
    def insert_appeal(self, repair: sqlite3.Row, evidence_hash: str, explanation: str, actor: str) -> sqlite3.Row:
        cur = self.conn.execute(
            """INSERT INTO repair_appeals(repair_id,recall_id,dealer_id,new_evidence_hash,explanation,status,submitted_by,submitted_at)
               VALUES(?,?,?,?,?, 'pending',?,?)""",
            (repair["id"], repair["recall_id"], repair["dealer_id"], evidence_hash, explanation, actor, _now()))
        return self.get_appeal(cur.lastrowid)

    def get_appeal(self, appeal_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM repair_appeals WHERE id=?", (appeal_id,)).fetchone()

    def pending_for_repair(self, repair_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM repair_appeals WHERE repair_id=? AND status='pending'", (repair_id,)).fetchone()

    def list_appeals(self) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM repair_appeals ORDER BY id"))

    def mark_decided(self, appeal: sqlite3.Row, status: str, actor: str, note: str) -> sqlite3.Row:
        self.conn.execute(
            "UPDATE repair_appeals SET status=?,decided_by=?,decided_at=?,decision_note=? WHERE id=?",
            (status, actor, _now(), note, appeal["id"]))
        return self.get_appeal(appeal["id"])

    # ---- 处理流水（只追加） ----
    def append_event(self, appeal_id: int, repair_id: int, actor: str, action: str, details: dict) -> None:
        self.conn.execute(
            "INSERT INTO appeal_events(appeal_id,repair_id,at,actor,action,details_json) VALUES(?,?,?,?,?,?)",
            (appeal_id, repair_id, _now(), actor, action, _j(details)))

    def events_for(self, appeal_id: int) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM appeal_events WHERE appeal_id=? ORDER BY id", (appeal_id,)))

    # ---- 库存缺口 ----
    def open_gap(self, recall_id: int, dealer_id: int, remedy_version: int, appeal_id: int, actor: str) -> int:
        cur = self.conn.execute(
            """INSERT INTO part_gaps(recall_id,dealer_id,remedy_version,quantity,filled,appeal_id,created_by,created_at)
               VALUES(?,?,?,1,0,?,?,?)""",
            (recall_id, dealer_id, remedy_version, appeal_id, actor, _now()))
        return cur.lastrowid

    def open_gaps(self, recall_id: int | None = None, dealer_id: int | None = None,
                  remedy_version: int | None = None) -> list[sqlite3.Row]:
        sql, args = "SELECT * FROM part_gaps WHERE filled<quantity", []
        if recall_id is not None: sql += " AND recall_id=?"; args.append(recall_id)
        if dealer_id is not None: sql += " AND dealer_id=?"; args.append(dealer_id)
        if remedy_version is not None: sql += " AND remedy_version=?"; args.append(remedy_version)
        return list(self.conn.execute(sql + " ORDER BY id", args))

    def fill_gap(self, gap: sqlite3.Row, quantity: int) -> int:
        used = min(quantity, int(gap["quantity"]) - int(gap["filled"]))
        self.conn.execute(
            """UPDATE part_gaps SET filled=filled+?,
                   resolved_at=CASE WHEN filled+?>=quantity THEN ? ELSE resolved_at END
               WHERE id=?""",
            (used, used, _now(), gap["id"]))
        return used
