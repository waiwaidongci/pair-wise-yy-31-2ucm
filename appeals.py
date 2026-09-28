"""打回申诉判定：网点按原维修单申诉、同单一份待审、监管维持/改判。

改判通过时原维修恢复完成；若退回的零件已被其他车辆领走，则登记库存缺口、
暂停该网点新维修，补货填平缺口后自动恢复。本模块只做判定，档案读写委托
records.AppealArchive。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from errors import ApiError
from records import AppealArchive


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class AppealService:
    def __init__(self, store):
        self.store, self.conn = store, store.conn
        self.archive = AppealArchive(store.conn)

    @staticmethod
    def _actor(actor: str | None, role: str | None, allowed: set[str]) -> str:
        if not actor: raise ApiError(401, "缺少身份")
        if role not in allowed: raise ApiError(403, "角色无权执行此操作")
        return actor

    def _repair(self, repair_id: int) -> sqlite3.Row:
        row = self.conn.execute("SELECT * FROM repairs WHERE id=?", (repair_id,)).fetchone()
        if not row: raise ApiError(404, "维修单不存在")
        return row

    # ---- 网点：按原维修单提交一次申诉 ----
    def submit_appeal(self, actor: str | None, role: str | None, repair_id: int,
                      new_evidence_hash: str, explanation: str) -> dict:
        actor = self._actor(actor, role, {"dealer"})
        new_evidence_hash, explanation = (new_evidence_hash or "").strip(), (explanation or "").strip()
        if not new_evidence_hash or not explanation:
            raise ApiError(400, "新证据哈希和情况说明不能为空")
        repair = self._repair(repair_id)
        if repair["dealer_id"] is None:
            raise ApiError(404, "维修单不存在")
        if repair["status"] != "flagged":
            raise ApiError(409, "只有被打回的维修单可以申诉")
        if self.archive.pending_for_repair(repair_id):
            raise ApiError(409, "同一维修单已有待审申诉")
        try:
            with self.conn:
                appeal = self.archive.insert_appeal(repair, new_evidence_hash, explanation, actor)
                self.archive.append_event(appeal["id"], repair_id, actor, "appeal.submit",
                                          {"new_evidence_hash": new_evidence_hash, "explanation": explanation})
                self.store.audit(actor, "appeal.submit", "repair_appeal", appeal["id"],
                                 {"repair_id": repair_id, "recall_id": repair["recall_id"]})
        except sqlite3.IntegrityError as exc:
            raise ApiError(409, "同一维修单已有待审申诉") from exc
        return self.dossier(appeal["id"])

    # ---- 监管：维持打回 / 改判通过 ----
    def decide_appeal(self, actor: str | None, role: str | None, appeal_id: int,
                      decision: str, note: str = "") -> dict:
        actor = self._actor(actor, role, {"regulator"})
        if decision not in {"uphold", "reverse"}:
            raise ApiError(400, "决定只能是 uphold（维持打回）或 reverse（改判通过）")
        appeal = self.archive.get_appeal(appeal_id)
        if not appeal: raise ApiError(404, "申诉不存在")
        if appeal["status"] != "pending": raise ApiError(409, "申诉已判定")
        repair = self._repair(appeal["repair_id"])
        if repair["status"] != "flagged": raise ApiError(409, "原维修单状态已变化")

        status = "upheld" if decision == "uphold" else "reversed"
        with self.conn:
            result = {"decision": decision}
            if status == "upheld":
                # 维持打回：原单继续作废，不触动库存
                self.archive.mark_decided(appeal, status, actor, note)
            else:
                result = self._restore_repair(appeal, repair, actor, note)
            self.archive.append_event(appeal["id"], repair["id"], actor, f"appeal.{status}",
                                      {"note": note, **result})
            self.store.audit(actor, f"appeal.{status}", "repair_appeal", appeal["id"],
                             {"repair_id": repair["id"], "note": note, **result})
        return self.dossier(appeal["id"])

    def _restore_repair(self, appeal: sqlite3.Row, repair: sqlite3.Row, actor: str, note: str) -> dict:
        """改判通过：原维修恢复完成；退回零件已被领走则登记缺口并暂停网点。"""
        result = {"part_outcome": None}
        # 1) 同一辆车已重新报修并完成的维修让位为被取代（其零件直接转给原单，不算缺口）
        successor = self.conn.execute(
            """SELECT * FROM repairs
               WHERE recall_id=? AND vehicle_id=? AND status='confirmed' AND id<>?""",
            (repair["recall_id"], repair["vehicle_id"], repair["id"])).fetchone()
        if successor:
            self.conn.execute(
                "UPDATE repairs SET status='superseded',review_note=? WHERE id=?",
                (f"原维修单 #{repair['id']} 申诉改判恢复完成，本单让位为被取代", successor["id"]))
            self.store.audit(actor, "repair.superseded", "repair", successor["id"],
                             {"restored_repair_id": repair["id"]})
            result["part_outcome"] = "transferred_from_superseded"
        else:
            part = self.conn.execute(
                "SELECT * FROM parts WHERE recall_id=? AND dealer_id=? AND remedy_version=?",
                (repair["recall_id"], repair["dealer_id"], repair["remedy_version"])).fetchone()
            if part and int(part["available"]) >= 1:
                # 2) 回库零件仍在：正常领用
                self.conn.execute("UPDATE parts SET available=available-1 WHERE id=? AND available>0", (part["id"],))
                result["part_outcome"] = "claimed_from_stock"
            else:
                # 3) 回库零件已被其他车辆领走：登记缺口、暂停网点新维修
                self.archive.open_gap(repair["recall_id"], repair["dealer_id"], repair["remedy_version"],
                                      appeal["id"], actor)
                self.conn.execute("UPDATE dealers SET active=0 WHERE id=?", (repair["dealer_id"],))
                result["part_outcome"] = "gap_dealer_suspended"
        review_note = repair["review_note"] or ""
        merged_note = (review_note + " | " if review_note else "") + f"申诉改判恢复完成：{note or '（无说明）'}"
        self.conn.execute(
            "UPDATE repairs SET status='confirmed',reviewed_by=?,reviewed_at=?,review_note=? WHERE id=?",
            (actor, _stamp(), merged_note, repair["id"]))
        self.archive.mark_decided(appeal, "reversed", actor, note)
        return result

    # ---- 补货：优先填平缺口，缺口清零自动恢复网点 ----
    def reserve_restock_for_gaps(self, recall_id: int, dealer_id: int, remedy_version: int, quantity: int) -> int:
        """入库时调用：返回用于填平缺口的数量，剩余数量才进入可售库存。"""
        remaining, resolved = quantity, []
        with self.conn:
            for gap in self.archive.open_gaps(recall_id, dealer_id, remedy_version):
                if remaining <= 0: break
                used = self.archive.fill_gap(gap, remaining)
                remaining -= used
                if used: resolved.append(gap["id"])
            if not self.archive.open_gaps(dealer_id=dealer_id):
                self.conn.execute("UPDATE dealers SET active=1 WHERE id=? AND active=0", (dealer_id,))
        return quantity - remaining

    # ---- 档案视图 ----
    def dossier(self, appeal_id: int) -> dict:
        appeal = self.archive.get_appeal(appeal_id)
        if not appeal: raise ApiError(404, "申诉不存在")
        repair = self._repair(appeal["repair_id"])
        vehicle = self.conn.execute("SELECT * FROM vehicles WHERE id=?", (repair["vehicle_id"],)).fetchone()
        part = self.conn.execute(
            "SELECT * FROM parts WHERE recall_id=? AND dealer_id=? AND remedy_version=?",
            (repair["recall_id"], repair["dealer_id"], repair["remedy_version"])).fetchone()
        open_gaps = self.archive.open_gaps(repair["recall_id"], repair["dealer_id"], repair["remedy_version"])
        dealer = self.conn.execute("SELECT * FROM dealers WHERE id=?", (repair["dealer_id"],)).fetchone()
        return {
            "appeal": dict(appeal),
            "repair": dict(repair),
            "vehicle": {"vin": vehicle["vin"], "model": vehicle["model"], "model_year": vehicle["model_year"]} if vehicle else None,
            "events": [dict(row) for row in self.archive.events_for(appeal_id)],
            "inventory": {
                "available": int(part["available"]) if part else 0,
                "open_gap_quantity": sum(int(g["quantity"]) - int(g["filled"]) for g in open_gaps),
                "open_gaps": [dict(g) for g in open_gaps],
                "dealer_active": bool(dealer["active"]) if dealer else False,
            },
        }

    def grouped(self, actor: str | None, role: str | None) -> dict:
        """页面：按待审、维持、改判列出申诉，保留原维修、每次处理与当前库存状态。"""
        self._actor(actor, role, {"dealer", "regulator", "manufacturer"})
        groups = {"pending": [], "upheld": [], "reversed": []}
        for appeal in self.archive.list_appeals():
            groups[appeal["status"]].append(self.dossier(appeal["id"]))
        return groups
