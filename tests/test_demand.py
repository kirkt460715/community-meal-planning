"""需求测算的业务规则测试：三分类、防重复计数、样本支持、替换意愿。"""
import unittest

from mealplan.records import (
    CellKey,
    InventoryCell,
    CellRecord,
    OrderEvent,
    OBS_CAPPED,
    OBS_UNOBSERVABLE,
    OBS_VISIBLE,
    STATE_SERVED,
    STATE_SUBSTITUTE,
    STATE_WAITLIST,
    build_cells,
    cell_demand_components,
    load_tables,
)
from mealplan import demand as dm


DATA = "data"


def make_event(order_id, *, site="S1", day="2026-08-03", window="early",
               requested="soft", issued="", served_site="",
               state=STATE_WAITLIST, permission="both"):
    return OrderEvent(
        order_id=order_id, day=day, booked_site=site, window=window,
        requested=requested, issued=issued, served_site=served_site,
        state=state, permission=permission,
    )


def make_cell(site="S1", day="2026-08-03", window="early", kind="soft",
              opening=5, closing=1, evidence="covered"):
    key = CellKey(site, day, window, kind)
    return CellRecord(InventoryCell(key, opening, closing, evidence))


class ObservationTests(unittest.TestCase):
    def test_three_observation_classes(self):
        visible = make_cell(closing=2, evidence="covered")
        capped = make_cell(closing=0, evidence="covered")
        partial = make_cell(closing=0, evidence="partial")
        self.assertEqual(visible.observation, OBS_VISIBLE)
        self.assertEqual(capped.observation, OBS_CAPPED)
        self.assertEqual(partial.observation, OBS_UNOBSERVABLE)

    def test_visible_cell_demand_is_fully_observed(self):
        cell = make_cell(closing=2)
        cell.booked_events.append(
            make_event("e1", state=STATE_SERVED, issued="soft", served_site="S1")
        )
        est = dm.estimate_cell(cell)
        self.assertEqual(est.lower, 1)
        self.assertEqual(est.estimate, 1.0)
        self.assertEqual(est.visible_samples, 1)
        self.assertEqual(est.proven_lost, 0)

    def test_partial_cell_only_lower_bound(self):
        cell = make_cell(evidence="partial", closing=0)
        cell.booked_events.append(
            make_event("e1", state=STATE_SERVED, issued="soft", served_site="S1")
        )
        est = dm.estimate_cell(cell)
        self.assertEqual(est.lower, 1)
        self.assertIsNone(est.estimate)
        self.assertTrue(any("partial" in flag for flag in est.flags))

    def test_capped_cell_with_waitlist_is_proven_lost(self):
        cell = make_cell(evidence="covered", closing=0)
        cell.booked_events.append(make_event("w1"))
        est = dm.estimate_cell(cell)
        self.assertEqual(est.proven_lost, 1)
        self.assertIsNone(est.estimate)
        self.assertTrue(any("候补" in flag for flag in est.flags))


class NoDoubleCountTests(unittest.TestCase):
    def test_substitution_counted_once_at_booked_cell(self):
        # 同一人 S1 订 soft，改在 S3 领 soft：原订格计 1 人，接收格只算 inbound
        source = make_cell(site="S1", kind="soft", closing=0)
        dest = make_cell(site="S3", kind="soft", opening=6, closing=5)
        event = make_event(
            "sub1", site="S1", requested="soft", issued="soft",
            served_site="S3", state=STATE_SUBSTITUTE
        )
        source.booked_events.append(event)
        dest.inbound_events.append(event)

        src = cell_demand_components(source)
        dst = cell_demand_components(dest)
        self.assertEqual(src["booked_demand"], 1)
        self.assertEqual(src["substituted"], 1)
        self.assertEqual(dst["booked_demand"], 0)
        self.assertEqual(dst["inbound"], 1)

    def test_meal_change_does_not_add_demand_to_issued_kind(self):
        soft_cell = make_cell(kind="soft", closing=0)
        regular_cell = make_cell(kind="regular", opening=8, closing=7)
        event = make_event(
            "sub2", requested="soft", issued="regular", served_site="S1",
            state=STATE_SUBSTITUTE
        )
        soft_cell.booked_events.append(event)
        regular_cell.inbound_events.append(event)
        self.assertEqual(cell_demand_components(soft_cell)["booked_demand"], 1)
        self.assertEqual(cell_demand_components(regular_cell)["booked_demand"], 0)

    def test_real_data_inventory_identity_and_constraints(self):
        tables = load_tables(DATA)
        cells, events, _ = build_cells(tables)
        # build_cells 内部已校验库存恒等式与菜单约束；这里再验证总量口径
        total_booked = sum(
            cell_demand_components(cell)["booked_demand"] for cell in cells.values()
        )
        self.assertEqual(total_booked, len(events))  # 每个居民恰好计一次
        restricted_subs = [
            e for e in events
            if e.requested == "restricted" and (e.meal_changed or e.site_changed)
        ]
        self.assertEqual(restricted_subs, [])


class SampleSupportTests(unittest.TestCase):
    def setUp(self):
        tables = load_tables(DATA)
        self.cells, _, _ = build_cells(tables)

    def test_estimate_never_below_evidence_lower_bound(self):
        for site in ("S1", "S2", "S3"):
            for window in ("early", "late"):
                for kind in ("regular", "soft", "restricted"):
                    est = dm.aggregate_demand(self.cells, site, window, kind)
                    if est.estimate is not None:
                        self.assertGreaterEqual(est.estimate, est.lower)

    def test_insufficient_visible_samples_returns_no_estimate(self):
        # 构造一个只有 2 个 visible 格的组（其余格通过过滤排除）
        tiny = {
            key: cell for key, cell in list(self.cells.items())[:0]
        }
        day_a, day_b = "2026-08-03", "2026-08-04"
        for day in (day_a, day_b):
            cell = make_cell(day=day, closing=1)
            tiny[cell.key] = cell
        est = dm.aggregate_demand(tiny)
        self.assertIsNone(est.estimate)
        self.assertTrue(any("样本支持不足" in f for f in est.flags))

    def test_willingness_excludes_restricted_and_visible_days(self):
        will = dm.substitution_willingness(self.cells, kind="restricted")
        self.assertEqual(will.eligible_events, 0)
        self.assertIsNone(will.rate)
        self.assertTrue(any("不可估计" in f for f in will.flags))

    def test_willingness_denominator_only_faces_stockout(self):
        will = dm.substitution_willingness(self.cells, kind="soft")
        self.assertGreater(will.eligible_events, 0)
        self.assertLessEqual(will.accepted_events, will.eligible_events)
        # 所有合格事件确实来自截断/离线格（visible 格居民未面临缺货，不进样本）；
        # 其中在售罄前买到的人仍是 served，属于"面临缺货但不需要替代"的分母
        eligible_ids = set()
        for cell in self.cells.values():
            if cell.key.kind != "soft" or cell.observation == OBS_VISIBLE:
                continue
            for event in cell.booked_events:
                if event.permission == "both":
                    eligible_ids.add(event.order_id)
        self.assertEqual(will.eligible_events, len(eligible_ids))
        accepted_ids = {
            event.order_id
            for cell in self.cells.values()
            for event in cell.booked_events
            if event.permission == "both"
            and (event.meal_changed or event.site_changed)
        }
        self.assertEqual(will.accepted_events, len(accepted_ids & eligible_ids))


if __name__ == "__main__":
    unittest.main()
