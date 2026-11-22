"""用过去真正能看到的情况回看建议，并分解误差来源。

回看（backtest）纪律：

- 对某一天做检验时，只能使用该天之前的数据；建议值由历史 ``visible`` 格产生，
  截断格（capped）和离线格（partial）的销量从不进入估计。
- 当天的真实值按可观测性区别对待：

  * ``visible`` 天：登记需求即真实需求，建议误差可以完整计算，并归入
    ``forecast_error``（纯预测偏差）。
  * ``capped`` 天：真实需求未知，登记需求只是下界，只能检验"建议是否足以覆盖已暴露
    需求"，欠备部分归入 ``sales_censored_error``——误差来自实销量受限，而不是模型。
  * ``unobservable`` 天：售罄时点不可知，只核对已暴露部分，结果标注
    ``unobservable_error``，不据此判断模型好坏。
- 集中活动日（calendar.event == market）单独标注，活动冲击不与普通日混算。

替代政策的回看：取消（候补未出餐）、推迟/改领（换点或换餐的 substitute）只在登记
允许（both）且餐种可替代时才算"政策可用的替代"；两处同时缺货时，同一批换点居民
在接收站不重复计为新增需求——本模块以订单号为单位追溯，天然只计一次。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .records import (
    CellKey,
    CellRecord,
    NON_SUBSTITUTABLE_KINDS,
    OBS_CAPPED,
    OBS_UNOBSERVABLE,
    OBS_VISIBLE,
    PERM_BOTH,
    OrderEvent,
    cell_demand_components,
)

# 误差来源标签
ERR_FORECAST = "forecast_error"            # 可完整观测天上的纯预测偏差
ERR_CENSORED = "sales_censored_error"      # 实销量受限（售罄截断）导致无法核对
ERR_UNOBSERVABLE = "unobservable_error"    # 离线导致无法核对
ERR_EVENT = "event_day"                    # 集中活动日，单独标注


@dataclass
class DayCellResult:
    day: str
    site: str
    window: str
    kind: str
    observation: str
    recommended: int
    observed_lower: int
    """当天登记需求下界（每个居民一次）。"""
    proven_lost: int
    error: int
    """推荐量 - 已暴露需求；负数为欠备。"""
    error_source: str
    event: str
    flags: list[str] = field(default_factory=list)


@dataclass
class BacktestSummary:
    results: list[DayCellResult]
    mean_forecast_error: float
    censored_shortfalls: int
    unobservable_cells: int
    event_day_cells: int
    policy_substitutions: int
    """政策可用替代（both 登记、非专用餐、实际换餐或换点）的事件数。"""
    blocked_restricted: int
    """专用餐退单/候补：制度上不可替餐，只计数，不推断替餐意愿。"""
    blocked_by_registration: int
    """非 both 登记（same_site/exact_only）的未兑现事件：替餐不在居民已登记选择内。"""

    @property
    def blocked_by_policy(self) -> int:
        return self.blocked_restricted + self.blocked_by_registration

    def error_source_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for result in self.results:
            counts[result.error_source] = counts.get(result.error_source, 0) + 1
        return counts


def _market_days(tables) -> set[str]:
    return {row["day"] for row in tables.get("calendar", []) if row.get("event") == "market"}


def run_backtest(
    cells: dict[CellKey, CellRecord],
    tables: dict[str, list[dict[str, str]]],
    min_history: int = 5,
) -> BacktestSummary:
    """逐日滚动回看：用截至前一天的 visible 格均值建议当天备货。

    建议量按同 站点×窗口×餐种 的历史 visible 格登记需求均值向上取整；历史 visible
    不足 ``min_history`` 时不给数字建议（推荐量为 0 并标注样本不足），避免拿截断销量
    凑数。
    """
    market = _market_days(tables)
    days = sorted({cell.key.day for cell in cells.values()})
    history: dict[tuple[str, str, str], list[int]] = {}

    results: list[DayCellResult] = []
    policy_substitutions = 0
    blocked_restricted = 0
    blocked_by_registration = 0

    for day in days:
        todays = [cell for cell in cells.values() if cell.key.day == day]
        for cell in todays:
            comp = cell_demand_components(cell)
            group = (cell.key.site, cell.key.window, cell.key.kind)
            past = history.get(group, [])
            flags: list[str] = []
            if len(past) < min_history:
                recommended = 0
                flags.append(
                    f"历史可观测样本 {len(past)} < {min_history}：不出数字建议"
                )
            else:
                recommended = -(-sum(past) // len(past))  # ceil(mean)

            observed_lower = comp["booked_demand"]
            proven_lost = comp["waitlist"]
            error = recommended - observed_lower

            if cell.observation == OBS_VISIBLE:
                source = ERR_FORECAST
            elif cell.observation == OBS_CAPPED:
                source = ERR_CENSORED
                flags.append(
                    "当天确认售罄：真实需求不可知，负误差只说明未覆盖已暴露需求"
                )
            else:
                source = ERR_UNOBSERVABLE
                flags.append("当天 partial 离线：售罄时点不可知，误差不可定性")
            if day in market:
                flags.append(ERR_EVENT)

            results.append(
                DayCellResult(
                    day=day,
                    site=cell.key.site,
                    window=cell.key.window,
                    kind=cell.key.kind,
                    observation=cell.observation,
                    recommended=recommended,
                    observed_lower=observed_lower,
                    proven_lost=proven_lost,
                    error=error,
                    error_source=source,
                    event="market" if day in market else "none",
                    flags=flags,
                )
            )

            # 替代政策核对（以订单号为单位，换点居民只在原订格出现一次）
            for event in cell.booked_events:
                if event.state == "waitlist":
                    if event.requested in NON_SUBSTITUTABLE_KINDS:
                        blocked_restricted += 1
                    elif event.permission != PERM_BOTH:
                        blocked_by_registration += 1
                if event.meal_changed or event.site_changed:
                    policy_substitutions += 1

        # 当天结束后，只有 visible 格能进入历史样本
        for cell in todays:
            if cell.observation == OBS_VISIBLE:
                group = (cell.key.site, cell.key.window, cell.key.kind)
                history.setdefault(group, []).append(
                    cell_demand_components(cell)["booked_demand"]
                )

    forecast_errors = [
        r.error for r in results if r.error_source == ERR_FORECAST and r.recommended > 0
    ]
    return BacktestSummary(
        results=results,
        mean_forecast_error=(
            sum(forecast_errors) / len(forecast_errors) if forecast_errors else 0.0
        ),
        censored_shortfalls=sum(
            1 for r in results if r.error_source == ERR_CENSORED and r.error < 0
        ),
        unobservable_cells=sum(
            1 for r in results if r.error_source == ERR_UNOBSERVABLE
        ),
        event_day_cells=sum(1 for r in results if ERR_EVENT in r.flags),
        policy_substitutions=policy_substitutions,
        blocked_restricted=blocked_restricted,
        blocked_by_registration=blocked_by_registration,
    )


def trace_substitution_chain(events: list[OrderEvent]) -> list[dict[str, object]]:
    """追溯每一条替代链，证明一次需求只计一人。

    输出每个 substitute 事件的原订格 -> 实发格去向，并标注两处同时缺货时该居民
    不会在接收站被重复计需求（接收格只记 inbound 库存消耗）。
    """
    chains: list[dict[str, object]] = []
    for event in events:
        if not (event.meal_changed or event.site_changed):
            continue
        chains.append(
            {
                "order_id": event.order_id,
                "day": event.day,
                "window": event.window,
                "booked": f"{event.booked_site}/{event.requested}",
                "issued": f"{event.served_site}/{event.issued}",
                "meal_changed": event.meal_changed,
                "site_changed": event.site_changed,
                "counted_at": f"{event.booked_site}/{event.requested}",
                "note": "需求只挂原订格；接收格仅计库存消耗，不新增需求人",
            }
        )
    return chains
