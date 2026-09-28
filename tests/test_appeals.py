import sys, tempfile, unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import ApiError, RecallService, Store


class AppealFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.s = RecallService(Store(Path(self.tmp.name) / "r.db"))
        self.dealer = self.s.register_dealer("reg", "regulator", "D-CN", "中国中心", "CN")
        r = self.s.create_recall("maker", "manufacturer", "RC-1", "制动检查", {"models": ["X"], "model_years": [2018], "vin_prefixes": ["LX"], "countries": ["CN"]}, {"version": 1, "description": "更换软管"})
        r = self.s.submit_recall("maker", "manufacturer", r["id"], r["revision"])
        self.recall = self.s.review_recall("reg", "regulator", r["id"], "publish", r["revision"], "同意发布")

    def tearDown(self): self.s.store.close(); self.tmp.cleanup()

    def flagged_repair(self, vin, stock=1):
        vehicle = self.s.register_vehicle("maker", "manufacturer", vin, "X", 2018, "CN", "车主")
        self.s.add_parts("maker", "manufacturer", self.recall["id"], self.dealer["id"], 1, stock)
        repair = self.s.report_repair("dealer", "dealer", self.recall["id"], vin, self.dealer["id"], 1, "ev1", False, idempotency_key=f"k-{vin}")
        flagged = self.s.review_repair("reg", "regulator", repair["id"], "confirm", "证据存疑，打回")
        self.assertEqual("flagged", flagged["status"])
        return vehicle, repair

    def test_archive_groups_and_single_pending(self):
        _, repair = self.flagged_repair("LX00001")
        # 非打回状态 / 非本网点不能申诉
        with self.assertRaises(ApiError):
            self.s.submit_appeal("other", "dealer", repair["id"], "new", "说明")
        with self.assertRaises(ApiError):
            self.s.submit_appeal("reg", "regulator", repair["id"], "new", "说明")
        appeal = self.s.submit_appeal("dealer", "dealer", repair["id"], "ev2", "补充检测照片")
        self.assertEqual("pending", appeal["status"])
        self.assertEqual("LX00001", appeal["vin"])
        self.assertEqual("flagged", appeal["original_repair"]["status"])
        with self.assertRaises(ApiError):  # 同单只能有一份待审
            self.s.submit_appeal("dealer", "dealer", repair["id"], "ev3", "再次申诉")
        archive = self.s.appeals_archive("reg", "regulator")
        self.assertEqual(1, len(archive["pending"]))
        self.assertEqual(0, len(archive["upheld"]))
        self.assertEqual(0, len(archive["overturned"]))
        only = self.s.appeals_archive("dealer", "dealer", "pending")["pending"]
        self.assertEqual(appeal["id"], only[0]["id"])

    def test_uphold_keeps_void_then_reappeal(self):
        _, repair = self.flagged_repair("LX00002")
        appeal = self.s.submit_appeal("dealer", "dealer", repair["id"], "ev2", "补充说明")
        upheld = self.s.decide_appeal("reg", "regulator", appeal["id"], "uphold", "证据仍不足")
        self.assertEqual("upheld", upheld["status"])
        self.assertEqual("flagged", upheld["original_repair"]["status"])  # 原单继续作废
        # 维持后可再次申诉
        second = self.s.submit_appeal("dealer", "dealer", repair["id"], "ev4", "新的检测报告")
        self.assertEqual("pending", second["status"])
        archive = self.s.appeals_archive("reg", "regulator")
        self.assertEqual(1, len(archive["upheld"]))
        self.assertEqual(1, len(archive["pending"]))
        # 每次处理都保留
        self.assertEqual(2, len(second["appeal_history"]))

    def test_overturn_restores_repair_consuming_returned_part(self):
        _, repair = self.flagged_repair("LX00003", stock=2)
        # 打回退库后库存=2；退回零件未被领走，改判直接扣回
        appeal = self.s.submit_appeal("dealer", "dealer", repair["id"], "ev2", "补充说明")
        overturned = self.s.decide_appeal("reg", "regulator", appeal["id"], "overturn", "新证据成立")
        self.assertEqual("overturned", overturned["status"])
        self.assertEqual("confirmed", overturned["original_repair"]["status"])
        self.assertEqual([], overturned["part_gaps"])
        part = self.s.conn.execute("SELECT available FROM parts WHERE id=1").fetchone()
        self.assertEqual(1, part["available"])
        self.assertFalse(overturned["part_gaps"])

    def test_gap_suspends_dealer_and_restock_auto_recovers(self):
        # 库存 1：打回退库 -> available 1；另一辆车先领走退回的零件 -> available 0
        v1, repair1 = self.flagged_repair("LX00010", stock=1)
        self.s.register_vehicle("maker", "manufacturer", "LX00011", "X", 2018, "CN", "车主二")
        second = self.s.report_repair("dealer", "dealer", self.recall["id"], "LX00011", self.dealer["id"], 1, "ev-other", False, idempotency_key="k-other")
        part = self.s.conn.execute("SELECT available FROM parts WHERE id=1").fetchone()
        self.assertEqual(0, part["available"])
        # 此时改判通过 -> 零件已被领走，登记库存缺口并暂停网点
        appeal = self.s.submit_appeal("dealer", "dealer", repair1["id"], "ev2", "补充说明")
        overturned = self.s.decide_appeal("reg", "regulator", appeal["id"], "overturn", "成立")
        self.assertEqual("confirmed", overturned["original_repair"]["status"])
        self.assertEqual("open", overturned["part_gaps"][0]["status"])
        with self.assertRaises(ApiError) as ctx:
            self.s.register_vehicle("maker", "manufacturer", "LX00012", "X", 2018, "CN", "车主三")
            self.s.report_repair("dealer", "dealer", self.recall["id"], "LX00012", self.dealer["id"], 1, "ev-x", False, idempotency_key="k-x")
        self.assertIn("库存缺口", ctx.exception.message)
        inv = self.s.appeals_archive("reg", "regulator")["inventory"]["dealers"][0]
        self.assertTrue(inv["new_repairs_suspended"])
        self.assertEqual(1, inv["open_gap_quantity"])
        self.assertEqual(0, inv["parts"][0]["available"])
        # 补货：1 件冲抵缺口（缺口关闭、自动恢复），余量 1 件入可用库存
        restock = self.s.add_parts("maker", "manufacturer", self.recall["id"], self.dealer["id"], 1, 2)
        self.assertFalse(restock["dealer_suspended"])  # 补货后自动恢复
        self.assertEqual(1, len(restock["recovered_gaps"]))
        self.assertEqual(1, restock["part"]["available"])
        # 恢复后可正常报修
        self.s.register_vehicle("maker", "manufacturer", "LX00013", "X", 2018, "CN", "车主三")
        new_repair = self.s.report_repair("dealer", "dealer", self.recall["id"], "LX00013", self.dealer["id"], 1, "ev-y", False, idempotency_key="k-y")
        self.assertEqual("reported", new_repair["status"])

    def test_overturn_blocked_when_new_repair_confirmed(self):
        _, repair = self.flagged_repair("LX00030", stock=3)
        appeal = self.s.submit_appeal("dealer", "dealer", repair["id"], "ev2", "申诉中")
        # 网点重新报修同车，且监管先确认了新维修
        new_repair = self.s.report_repair("dealer", "dealer", self.recall["id"], "LX00030", self.dealer["id"], 1, "ev-new", True, idempotency_key="k-new")
        self.s.review_repair("reg", "regulator", new_repair["id"], "confirm", "新单通过")
        with self.assertRaises(ApiError):
            self.s.decide_appeal("reg", "regulator", appeal["id"], "overturn")
        again = self.s._appeal_dict(self.s._row("repair_appeals", appeal["id"]))
        self.assertEqual("upheld", again["status"])
        self.assertEqual("flagged", again["original_repair"]["status"])

    def test_decide_requires_regulator_and_pending(self):
        _, repair = self.flagged_repair("LX00020", stock=1)
        appeal = self.s.submit_appeal("dealer", "dealer", repair["id"], "ev2", "说明")
        with self.assertRaises(ApiError):
            self.s.decide_appeal("dealer", "dealer", appeal["id"], "overturn")
        self.s.decide_appeal("reg", "regulator", appeal["id"], "uphold")
        with self.assertRaises(ApiError):
            self.s.decide_appeal("reg", "regulator", appeal["id"], "overturn")


if __name__ == "__main__": unittest.main()
