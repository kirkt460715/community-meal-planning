"""兑现追踪测试：分批确认、停做改约、去重与余量承接。"""
import unittest

from mealplan.records import load_tables
from mealplan import allocation, fulfillment
from mealplan.allocation import build_plan


DATA = "data"


class FulfillmentTests(unittest.TestCase):
    def setUp(self):
        self.tables = load_tables(DATA)
        self.plan = build_plan(
            self.tables, {("S1", "early", "soft"): 4}
        )

    def test_planned_commitment_passes_capacity_check(self):
        ok, detail = fulfillment.confirm_commitment(self.tables, "P3")
        self.assertTrue(ok, detail)
        self.assertIn("P3", detail)

    def test_unknown_commitment_rejected(self):
        ok, _ = fulfillment.confirm_commitment(self.tables, "NOPE")
        self.assertFalse(ok)

    def test_restricted_residents_always_contact_reschedule(self):
        affected = [
            {
                "order_ref": f"r{i}",
                "site": "S1",
                "window": "early",
                "requested_kind": "restricted",
                "permission": "both",
            }
            for i in range(3)
        ]
        options = fulfillment.handle_stop_and_residents(
            self.tables, self.plan, affected
        )
        self.assertEqual(len(options), 3)
        self.assertTrue(all(o.status == "contact_reschedule" for o in options))
        self.assertTrue(all("专用餐" in o.detail for o in options))

    def test_exact_only_registration_blocks_substitution(self):
        affected = [
            {
                "order_ref": "ex1",
                "site": "S1",
                "window": "early",
                "requested_kind": "soft",
                "permission": "exact_only",
            }
        ]
        options = fulfillment.handle_stop_and_residents(
            self.tables, self.plan, affected
        )
        self.assertEqual(options[0].status, "contact_reschedule")
        self.assertIn("exact_only", options[0].detail)

    def test_duplicate_order_refs_handled_once(self):
        # 两处同时缺货，同一批换点居民不能被两个点各计一次
        affected = [
            {
                "order_ref": "dup",
                "site": "S1",
                "window": "early",
                "requested_kind": "soft",
                "permission": "both",
            }
        ] * 4
        options = fulfillment.handle_stop_and_residents(
            self.tables, self.plan, affected
        )
        self.assertEqual(len(options), 1)

    def test_both_permission_gets_meal_or_site_option_or_reschedule(self):
        affected = [
            {
                "order_ref": f"b{i}",
                "site": "S1",
                "window": "early",
                "requested_kind": "soft",
                "permission": "both",
            }
            for i in range(6)
        ]
        options = fulfillment.handle_stop_and_residents(
            self.tables, self.plan, affected
        )
        statuses = {o.status for o in options}
        self.assertTrue(
            statuses <= {"substitute_meal", "relocate", "contact_reschedule"}
        )
        # 任何换点/换餐结果都必须注明原订单只计一次
        for option in options:
            if option.status in ("substitute_meal", "relocate"):
                self.assertIn("只计一次", option.detail)

    def test_late_demand_uses_only_spare_capacity(self):
        handling = fulfillment.handle_late_demand(self.tables, self.plan)
        record = next(h for h in handling if h["change_id"] == "C4")
        self.assertEqual(record["requested"], 4)
        self.assertGreaterEqual(record["unmet"], 0)
        self.assertEqual(
            record["accepted_with_spare"] + record["unmet"], record["requested"]
        )
        # restricted 承接不了时必须给出政策原因
        if record["unmet"]:
            self.assertTrue(
                any("专用餐" in b for b in record["blockers"])
            )

    def test_full_report_accepts_existing_commitments(self):
        report = fulfillment.build_fulfillment_report(self.tables, self.plan, [])
        self.assertEqual(set(report.accepted_commitments), {"P1", "P2", "P4"})
        # P3 容量检验通过，因此不会出现在拒绝清单
        rejected_ids = {item["commitment"] for item in report.rejected_confirmations}
        self.assertNotIn("P3", rejected_ids)


if __name__ == "__main__":
    unittest.main()
