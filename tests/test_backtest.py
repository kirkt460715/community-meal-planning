"""回看检验测试：只用历史 visible 格、误差来源分解、替代链追溯。"""
import unittest

from mealplan.records import load_tables, build_cells, OBS_VISIBLE
from mealplan import backtest


DATA = "data"


class BacktestTests(unittest.TestCase):
    def setUp(self):
        self.tables = load_tables(DATA)
        self.cells, self.events, _ = build_cells(self.tables)
        self.summary = backtest.run_backtest(self.cells, self.tables)

    def test_every_cell_classified_into_one_error_source(self):
        counts = self.summary.error_source_counts()
        self.assertEqual(
            sum(counts.values()), len(self.cells)
        )
        self.assertEqual(
            counts[backtest.ERR_UNOBSERVABLE],
            sum(1 for c in self.cells.values() if c.observation == "unobservable"),
        )

    def test_history_only_uses_visible_cells(self):
        # 所有进入 forecast 类别的结果，其当天格必然是 visible
        forecast_days = {
            (r.day, r.site, r.window, r.kind)
            for r in self.summary.results
            if r.error_source == backtest.ERR_FORECAST
        }
        for day, site, window, kind in forecast_days:
            from mealplan.records import CellKey

            cell = self.cells[CellKey(site, day, window, kind)]
            self.assertEqual(cell.observation, OBS_VISIBLE)

    def test_censored_shortfalls_are_not_called_forecast_errors(self):
        for result in self.summary.results:
            if result.error_source == backtest.ERR_CENSORED:
                self.assertTrue(
                    any("售罄" in flag for flag in result.flags)
                )

    def test_substitution_chains_trace_single_person(self):
        chains = backtest.trace_substitution_chain(self.events)
        substitute_events = [
            e for e in self.events if e.meal_changed or e.site_changed
        ]
        self.assertEqual(len(chains), len(substitute_events))
        ids = [chain["order_id"] for chain in chains]
        self.assertEqual(len(ids), len(set(ids)))  # 订单号唯一，无重复计人
        for chain in chains:
            self.assertNotEqual(chain["booked"], chain["issued"])
            self.assertIn("不新增需求人", chain["note"])

    def test_policy_blocked_split_between_restricted_and_registration(self):
        # 183 笔 restricted 候补全部计入制度阻断；其余 waitlist 按登记选择区分
        self.assertEqual(
            self.summary.blocked_restricted,
            sum(
                1
                for e in self.events
                if e.state == "waitlist" and e.requested == "restricted"
            ),
        )
        self.assertGreaterEqual(self.summary.blocked_by_registration, 0)
        self.assertEqual(
            self.summary.blocked_by_policy,
            self.summary.blocked_restricted
            + self.summary.blocked_by_registration,
        )

    def test_market_days_flagged_separately(self):
        self.assertGreater(self.summary.event_day_cells, 0)


if __name__ == "__main__":
    unittest.main()
