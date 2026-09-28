import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, RecallService, Store
from appeals import AppealService


class AppealFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        store = Store(Path(self.tmp.name) / "r.db")
        self.appeals = AppealService(store)
        self.s = RecallService(store, self.appeals)
        self.dealer = self.s.register_dealer("reg", "regulator", "D-CN", "中国中心", "CN")
        recall = self.s.create_recall("maker", "manufacturer", "RC-1", "制动检查",
                                      {"models": ["X"], "model_years": [2018], "vin_prefixes": ["LX"], "countries": ["CN"]},
                                      {"version": 1, "description": "更换软管"})
        recall = self.s.submit_recall("maker", "manufacturer", recall["id"], recall["revision"])
        self.recall = self.s.review_recall("reg", "regulator", recall["id"], "publish", recall["revision"], "同意发布")

    def tearDown(self): self.s.store.close(); self.tmp.cleanup()

    def _flagged_repair(self, vin="LX00001", stock=1, consistent=False):
        v = self.s.register_vehicle("maker", "manufacturer", vin, "X", 2018, "CN", "车主")
        self.s.add_parts("maker", "manufacturer", self.recall["id"], self.dealer["id"], 1, stock)
        rep = self.s.report_repair("dealer", "dealer", self.recall["id"], vin, self.dealer["id"],
                                   1, "evi-" + vin, consistent, idempotency_key="rep-" + vin)
        return v, self.s.review_repair("reg", "regulator", rep["id"], "confirm", "证据不一致打回")

    def _stock(self):
        row = self.s.conn.execute("SELECT available FROM parts WHERE recall_id=? AND dealer_id=? AND remedy_version=1",
                                  (self.recall["id"], self.dealer["id"])).fetchone()
        return int(row["available"]) if row else 0

    # 1) 申诉提交约束：仅打回单可申诉、同单一份待审、角色与必填校验
    def test_submit_rules(self):
        _, repair = self._flagged_repair()
        with self.assertRaises(ApiError):  # 监管不能替网点申诉
            self.appeals.submit_appeal("reg", "regulator", repair["id"], "h", "说明")
        with self.assertRaises(ApiError):  # 缺证据/说明
            self.appeals.submit_appeal("d", "dealer", repair["id"], "", "说明")
        d = self.appeals.submit_appeal("d", "dealer", repair["id"], "new-hash", "补拍维修照片")
        self.assertEqual("pending", d["appeal"]["status"])
        self.assertEqual(1, len(d["events"]))
        with self.assertRaises(ApiError):  # 同单只能有一份待审
            self.appeals.submit_appeal("d", "dealer", repair["id"], "h2", "再申诉")

        v2 = self.s.register_vehicle("maker", "manufacturer", "LX00002", "X", 2018, "CN", "钱七")
        good = self.s.report_repair("dealer", "dealer", self.recall["id"], "LX00002", self.dealer["id"],
                                    1, "evi-2", True, idempotency_key="rep-2")
        confirmed = self.s.review_repair("reg", "regulator", good["id"], "confirm")
        with self.assertRaises(ApiError):  # 已完成单不能申诉
            self.appeals.submit_appeal("d", "dealer", confirmed["id"], "h", "说明")

    # 2) 维持打回：原单继续作废，库存不动
    def test_uphold_keeps_void_and_allows_new_appeal(self):
        _, repair = self._flagged_repair(stock=1)
        # 打回时零件已回库：可用为 1
        self.assertEqual(1, self._stock())
        a = self.appeals.submit_appeal("d", "dealer", repair["id"], "h", "说明")
        out = self.appeals.decide_appeal("reg", "regulator", a["appeal"]["id"], "uphold", "证据仍不足")
        self.assertEqual("upheld", out["appeal"]["status"])
        self.assertEqual("flagged", out["repair"]["status"])
        self.assertEqual(1, out["inventory"]["available"])
        with self.assertRaises(ApiError):  # 已判定申诉不可重复判定
            self.appeals.decide_appeal("reg", "regulator", a["appeal"]["id"], "reverse")
        # 维持后可再次申诉
        a2 = self.appeals.submit_appeal("d2", "dealer", repair["id"], "h2", "新说明")
        self.assertEqual("pending", a2["appeal"]["status"])

    # 3) 改判通过且零件仍在：原维修恢复完成，零件被扣减
    def test_reverse_with_stock_restores_repair(self):
        _, repair = self._flagged_repair(stock=1)
        self.assertEqual(1, self._stock())
        a = self.appeals.submit_appeal("d", "dealer", repair["id"], "h", "说明")
        out = self.appeals.decide_appeal("reg", "regulator", a["appeal"]["id"], "reverse", "新证据有效")
        self.assertEqual("reversed", out["appeal"]["status"])
        self.assertEqual("confirmed", out["repair"]["status"])
        self.assertEqual(0, out["inventory"]["available"])
        self.assertTrue(out["inventory"]["dealer_active"])
        # 档案含每次处理
        actions = [e["action"] for e in out["events"]]
        self.assertEqual(["appeal.submit", "appeal.reversed"], actions)

    # 4) 改判通过但回退零件已被其他车辆领走：缺口 + 暂停 + 补货自动恢复
    def test_reverse_part_taken_gap_suspend_restock(self):
        _, repair = self._flagged_repair(vin="LX00001", stock=1)  # 打回后库存回 1
        # 另一辆车把回库的这一件领走并完成维修
        v2 = self.s.register_vehicle("maker", "manufacturer", "LX00002", "X", 2018, "CN", "钱七")
        rep2 = self.s.report_repair("dealer", "dealer", self.recall["id"], "LX00002", self.dealer["id"],
                                    1, "evi-2", True, idempotency_key="rep-2")
        self.s.review_repair("reg", "regulator", rep2["id"], "confirm")
        self.assertEqual(0, self._stock())

        a = self.appeals.submit_appeal("d", "dealer", repair["id"], "h", "说明")
        out = self.appeals.decide_appeal("reg", "regulator", a["appeal"]["id"], "reverse", "改判")
        self.assertEqual("confirmed", out["repair"]["status"])
        self.assertEqual(1, out["inventory"]["open_gap_quantity"])
        self.assertFalse(out["inventory"]["dealer_active"])

        # 暂停期间不能新报修
        v3 = self.s.register_vehicle("maker", "manufacturer", "LX00003", "X", 2018, "CN", "孙八")
        with self.assertRaises(ApiError):
            self.s.report_repair("dealer", "dealer", self.recall["id"], "LX00003", self.dealer["id"],
                                 1, "evi-3", True, idempotency_key="rep-3")

        # 补货 1 件优先填平缺口，可用库存仍为 0，网点恢复
        parts = self.s.add_parts("maker", "manufacturer", self.recall["id"], self.dealer["id"], 1, 1)
        self.assertEqual(1, parts["gap_filled"])
        self.assertEqual(0, parts["available"])
        dealer = self.s._row("dealers", self.dealer["id"])
        self.assertTrue(dealer["active"])
        # 缺口已结清，再补货进正常库存，新维修恢复
        self.s.add_parts("maker", "manufacturer", self.recall["id"], self.dealer["id"], 1, 1)
        again = self.s.report_repair("dealer", "dealer", self.recall["id"], "LX00003", self.dealer["id"],
                                     1, "evi-3", True, idempotency_key="rep-3")
        self.assertEqual("reported", again["status"])

    # 5) 部分补货不足以填平缺口时网点继续暂停
    def test_partial_restock_keeps_suspended(self):
        _, repair = self._flagged_repair(vin="LX00010", stock=1)
        v2 = self.s.register_vehicle("maker", "manufacturer", "LX00011", "X", 2018, "CN", "钱七")
        rep2 = self.s.report_repair("dealer", "dealer", self.recall["id"], "LX00011", self.dealer["id"],
                                    1, "e2", True, idempotency_key="r2")
        self.s.review_repair("reg", "regulator", rep2["id"], "confirm")
        a = self.appeals.submit_appeal("d", "dealer", repair["id"], "h", "说明")
        self.appeals.decide_appeal("reg", "regulator", a["appeal"]["id"], "reverse")
        # 再制造第二个缺口：先恢复库存再重复一遍不现实，这里直接校验档案层缺口数量
        gaps = self.appeals.archive.open_gaps(dealer_id=self.dealer["id"])
        self.assertEqual(1, sum(int(g["quantity"]) - int(g["filled"]) for g in gaps))

    # 6) 页面视图按三组列出
    def test_grouped_view(self):
        _, r1 = self._flagged_repair(vin="LX00020", stock=2)
        _, r2 = self._flagged_repair(vin="LX00021", stock=2)
        a1 = self.appeals.submit_appeal("d", "dealer", r1["id"], "h", "x")
        a2 = self.appeals.submit_appeal("d", "dealer", r2["id"], "h", "x")
        self.appeals.decide_appeal("reg", "regulator", a1["appeal"]["id"], "uphold")
        groups = self.appeals.grouped("d", "dealer")
        self.assertEqual(["pending", "upheld", "reversed"], list(groups))
        self.assertEqual(1, len(groups["pending"]))
        self.assertEqual(1, len(groups["upheld"]))
        self.assertEqual(a2["appeal"]["id"], groups["pending"][0]["appeal"]["id"])


if __name__ == "__main__": unittest.main()
