"""分单约束测试：整批、产线、线路、车辆、停业窗口、保底与停做。"""
import math
import unittest

from mealplan.records import load_tables
from mealplan import allocation
from mealplan.allocation import build_plan, remaining_capacity


DATA = "data"


class PlanConstraintTests(unittest.TestCase):
    def setUp(self):
        self.tables = load_tables(DATA)
        self.options, self.routes, self.commitments, self.changes = (
            allocation._parse_tables(self.tables)
        )
        self.plan = build_plan(self.tables, self._demands())

    @staticmethod
    def _demands():
        return {
            ("S1", "early", "regular"): 8,
            ("S1", "early", "soft"): 4,
            ("S1", "early", "restricted"): 7,
            ("S3", "early", "regular"): 8,
            ("S3", "early", "soft"): 5,
            ("S3", "early", "restricted"): 12,
            ("S2", "late", "regular"): 8,
            ("S2", "late", "soft"): 4,
        }

    def test_batch_and_line_caps_held(self):
        from collections import defaultdict

        per_prod = defaultdict(int)
        per_line = defaultdict(int)
        for line in self.plan.lines:
            option = next(
                o for o in self.options if o.production_id == line.production_id
            )
            per_prod[line.production_id] += line.portions
            per_line[(option.kitchen, option.line_batches)] += line.batches
            self.assertEqual(
                line.batches, math.ceil(line.portions / option.batch_size)
            )
        for option in self.options:
            dropped = any(
                c.change == "capacity_drop"
                and c.reference == option.kitchen
                and c.kind == option.kind
                for c in self.changes
            )
            cap = 0 if dropped else option.max_batches
            self.assertLessEqual(
                math.ceil(per_prod[option.production_id] / option.batch_size), cap
            )
            if dropped:
                self.assertEqual(per_prod[option.production_id], 0)
        for (kitchen, line_cap), used in per_line.items():
            self.assertLessEqual(used, line_cap)

    def test_route_and_vehicle_caps_held(self):
        from collections import defaultdict

        route_used = defaultdict(int)
        vehicle_used = defaultdict(int)
        for line in self.plan.lines:
            route = next(r for r in self.routes if r.route_id == line.route_id)
            route_used[line.route_id] += line.portions
            vehicle_used[(route.vehicle, line.window)] += line.portions
        for route in self.routes:
            self.assertLessEqual(route_used[route.route_id], route.capacity)
        for (vehicle, window), used in vehicle_used.items():
            cap = next(
                r.vehicle_capacity
                for r in self.routes
                if r.vehicle == vehicle and r.window == window
            )
            self.assertLessEqual(used, cap)

    def test_accepted_commitments_consume_capacity_first(self):
        accepted = [c for c in self.commitments if c.state == "accepted"]
        for commitment in accepted:
            matched = [
                line for line in self.plan.lines
                if commitment.commitment_id in line.reason
            ]
            self.assertEqual(len(matched), 1)
            self.assertEqual(matched[0].portions, commitment.units)

    def test_capacity_drop_stops_soft_at_k1(self):
        # K1 soft 被停做：任何落地行都不能使用 K1B
        self.assertNotIn(
            "K1B", {line.production_id for line in self.plan.lines}
        )
        self.assertTrue(any("临时停做" in note for note in self.plan.notes))

    def test_restricted_shortfall_never_substituted(self):
        restricted_unmet = [
            item for item in self.plan.unmet if item["kind"] == "restricted"
        ]
        self.assertTrue(restricted_unmet)
        for item in restricted_unmet:
            self.assertIn("专用餐不可替餐", item["reason"])

    def test_minimum_and_late_demand_merged_not_duplicated(self):
        # S3/early/restricted 同时有 minimum(8)、late_demand(4) 与常规需求：
        # 队列里该格只能出现一次聚合条目
        s3_restricted_lines = [
            line for line in self.plan.lines
            if line.site == "S3" and line.window == "early"
            and line.kind == "restricted"
        ]
        reasons = "；".join(line.reason for line in s3_restricted_lines)
        self.assertLessEqual(
            len([line for line in s3_restricted_lines if "保底" in line.reason]), 1
        )
        self.assertIn("minimum", reasons)

    def test_historical_closed_window_does_not_block_next_day(self):
        # S2 在 2026-09-01 停业过，但次日目标日 S2 窗口正常，不应报停业
        unmet = [item for item in self.plan.unmet if item["site"] == "S2"]
        self.assertFalse(any("停业" in item["reason"] for item in unmet))

    def test_remaining_capacity_non_negative(self):
        spare = remaining_capacity(self.tables)
        self.assertTrue(all(v >= 0 for v in spare["production_batches_left"].values()))
        self.assertTrue(all(v >= 0 for v in spare["route_portions_left"].values()))
        self.assertTrue(
            all(v >= 0 for v in spare["vehicle_window_portions_left"].values())
        )

    def test_small_take_does_not_overconsume_batches(self):
        # 需求份数小于一个整批时，只允许占 1 批而不是按容量反推多占
        tiny = build_plan(self.tables, {("S2", "late", "soft"): 1})
        soft_lines = [
            line for line in tiny.lines
            if line.site == "S2" and line.window == "late" and line.kind == "soft"
        ]
        self.assertEqual(len(soft_lines), 1)
        self.assertEqual(soft_lines[0].batches, 1)
        self.assertLessEqual(soft_lines[0].portions, 3)  # K2B batch_size=3


if __name__ == "__main__":
    unittest.main()
