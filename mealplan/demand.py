"""需求量与替换意愿测算：只使用真正可观测的证据。

三类记录的区分（题目要求一一对应）：

1. **确实有机会但没人买**：``visible`` 格（covered 且收档有余量）。这类格的登记需求
   就是真实需求的完整观测，未售出的份数是被错过的可售机会，不是缺口。

2. **无法观察到需求**：``unobservable`` 格（partial，期间离线）。只能确认至少发生过
   ``sold`` 份需求，售罄时点与之后的潜在需求均不可知，禁止把销量当作需求，也禁止用
   其他格的均值替它补数。

3. **可证实的缺货失单**：``capped`` 格中登记为候补（waitlist）且未出餐的记录。候补只
   覆盖确有登记的人，不声明全体有需求者；没有候补记录的 capped 格只能给出需求下界。

防重复计数规则：

- 一次原订未兑现、后来改领替代品（``substitute``）的需求只挂在**原订格**上，计为一个
  人的需求和一次替换意愿；流入格只增加库存消耗，不再增加一个需求人。
- 两处同时缺货时，换点居民是同一批人：换点事件的需求只属于原订站点，不能在原站和
  接收站各算一次新增需求。
- 只能领 ``restricted`` 专用餐的居民（``exact_only``）没有任何替换事件；其退单/候补
  不进入替换意愿样本。

输出统一为区间口径：``lower``（证据支持的需求下界）、``estimate``（仅在样本支持充分
时给出的点估计，否则为 ``None``）、``unobserved``（不可观测的部分是否存在）。
"""
from __future__ import annotations

from dataclasses import dataclass

from .records import (
    CellKey,
    CellRecord,
    OBS_CAPPED,
    OBS_UNOBSERVABLE,
    OBS_VISIBLE,
    OrderEvent,
    PERM_BOTH,
    cell_demand_components,
)

# 一个聚合组至少需要这么多个可完整观测（visible）的格子，才允许输出点估计
MIN_VISIBLE_SAMPLES = 5


@dataclass
class DemandEstimate:
    """一个聚合组（格或 站点×时段×餐种 等）的需求测算结果。"""

    label: str
    lower: int
    """证据支持的需求下界（人数，每个居民只计一次）。"""
    estimate: float | None
    """样本支持充分时的点估计；否则为 None，调用方必须按区间展示。"""
    visible_samples: int
    """组内可完整观测需求的格子数。"""
    capped_samples: int
    unobservable_samples: int
    observed_demand: int
    """可完整观测格子上的登记需求合计（点估计只用这部分数据）。"""
    proven_lost: int
    """可证实的缺货失单人数（候补未出餐）。"""
    substituted: int
    """原订未兑现后改领替代品的人数（替换意愿的证据条数）。"""
    flags: list[str]
    """样本不足、离线、截断等必须向负责人明示的提示。"""

    @property
    def supported(self) -> bool:
        """样本是否支持点估计。"""
        return self.estimate is not None


@dataclass
class WillingnessEstimate:
    """替换意愿测算：分子分母都只统计真正发生过选择的人。"""

    label: str
    eligible_events: int
    """面临原订格缺货、且登记允许替代（both）的事件数。"""
    accepted_events: int
    """其中实际改领替代品（换餐或换点）的事件数。"""
    rate: float | None
    flags: list[str]

    @property
    def supported(self) -> bool:
        return self.rate is not None


def estimate_cell(cell: CellRecord) -> DemandEstimate:
    """单个库存格的需求测算。"""
    comp = cell_demand_components(cell)
    booked = comp["booked_demand"]
    proven_lost = comp["waitlist"]
    substituted = comp["substituted"]
    flags: list[str] = []

    visible = capped = unobs = 0
    observed_demand = 0
    if cell.observation == OBS_VISIBLE:
        visible = 1
        observed_demand = booked
        lower = booked
        estimate = float(booked)
    elif cell.observation == OBS_UNOBSERVABLE:
        unobs = 1
        # 离线期间售罄时点不可知：销量只是下界，登记需求同样只是已暴露部分
        lower = booked
        estimate = None
        flags.append("partial 离线格：售罄时点不可知，需求仅有下界，不出点估计")
    else:  # OBS_CAPPED
        capped = 1
        lower = booked
        estimate = None
        if proven_lost:
            flags.append(
                f"确认售罄且有 {proven_lost} 笔候补未出餐：可证实失单，"
                "未登记的潜在需求不可知"
            )
        else:
            flags.append("确认售罄但无候补记录：需求仅有下界，失单规模不可证实")

    return DemandEstimate(
        label="|".join(cell.key.as_tuple()),
        lower=lower,
        estimate=estimate,
        visible_samples=visible,
        capped_samples=capped,
        unobservable_samples=unobs,
        observed_demand=observed_demand,
        proven_lost=proven_lost,
        substituted=substituted,
        flags=flags,
    )


def _group_key(site: str | None, window: str | None, kind: str | None):
    return (site or "*", window or "*", kind or "*")


def aggregate_demand(
    cells: dict[CellKey, CellRecord],
    site: str | None = None,
    window: str | None = None,
    kind: str | None = None,
) -> DemandEstimate:
    """按 站点/配送窗口/餐种 的任意组合聚合需求。

    点估计只取组内 ``visible`` 格的均值再按全部天数展开；只要组内存在 partial 格或
    visible 样本不足，就降级为只给下界并写明原因。capped 格的销量从不参与均值，
    以免把"饱和时卖出的品种"误当成需求水平。
    """
    members = [
        cell
        for cell in cells.values()
        if (site is None or cell.key.site == site)
        and (window is None or cell.key.window == window)
        and (kind is None or cell.key.kind == kind)
    ]

    visible = capped = unobs = 0
    observed_demand = 0
    lower_total = 0
    proven_lost = 0
    substituted = 0
    flags: list[str] = []

    for cell in members:
        est = estimate_cell(cell)
        lower_total += est.lower
        proven_lost += est.proven_lost
        substituted += est.substituted
        if cell.observation == OBS_VISIBLE:
            visible += 1
            observed_demand += est.lower
        elif cell.observation == OBS_UNOBSERVABLE:
            unobs += 1
        else:
            capped += 1

    estimate: float | None = None
    if visible < MIN_VISIBLE_SAMPLES:
        flags.append(
            f"可完整观测样本仅 {visible} 格（阈值 {MIN_VISIBLE_SAMPLES}）：样本支持不足，"
            "只给需求下界"
        )
    else:
        # 点估计只从未截断格外推，但任何估计都不得低于已暴露需求的证据下界
        raw_estimate = observed_demand / visible * len(members)
        estimate = max(raw_estimate, float(lower_total))
        if raw_estimate < lower_total:
            flags.append(
                "未截断格外推值低于已暴露需求下界，估计已抬升至下界"
                "（截断/离线格的潜在需求仍未计入）"
            )
        if capped:
            flags.append(
                f"组内 {capped} 格确认售罄、{unobs} 格离线：点估计仅外推自 "
                f"{visible} 个未截断格，截断格实际需求不低于下界"
            )
    if unobs and estimate is not None:
        flags.append("含 partial 离线格：这些天的需求不可观测，点估计可能低估")

    return DemandEstimate(
        label="|".join(_group_key(site, window, kind)),
        lower=lower_total,
        estimate=estimate,
        visible_samples=visible,
        capped_samples=capped,
        unobservable_samples=unobs,
        observed_demand=observed_demand,
        proven_lost=proven_lost,
        substituted=substituted,
        flags=flags,
    )


def substitution_willingness(
    cells: dict[CellKey, CellRecord],
    site: str | None = None,
    window: str | None = None,
    kind: str | None = None,
) -> WillingnessEstimate:
    """替换意愿：只在原订格确认缺货、且登记允许替代的人群上计算。

    合格事件（分母）= 原订格 capped/unobservable（原订未能在本格足额兑现）且
    ``permission == both`` 的登记；接受事件（分子）= 其中实际发生换餐或换点的事件。
    专用餐（restricted）天然不产生合格事件，因此其退单不会被当成替餐意愿。
    """
    eligible: list[OrderEvent] = []
    accepted: list[OrderEvent] = []
    for cell in cells.values():
        if (site is not None and cell.key.site != site) or (
            window is not None and cell.key.window != window
        ) or (kind is not None and cell.key.kind != kind):
            continue
        if cell.observation == OBS_VISIBLE:
            continue  # 未缺货：居民没有面临替代选择，不进入意愿样本
        for event in cell.booked_events:
            if event.permission != PERM_BOTH:
                continue
            eligible.append(event)
            if event.meal_changed or event.site_changed:
                accepted.append(event)

    flags: list[str] = []
    rate: float | None = None
    if not eligible:
        flags.append("没有面临缺货且允许替代的登记：替换意愿不可估计")
    elif len(eligible) < MIN_VISIBLE_SAMPLES:
        flags.append(
            f"替代选择样本仅 {len(eligible)} 条（阈值 {MIN_VISIBLE_SAMPLES}）："
            "意愿率样本支持不足，只列计数"
        )
    else:
        rate = len(accepted) / len(eligible)
        if any(
            cell.observation == OBS_UNOBSERVABLE
            for cell in cells.values()
            if (site is None or cell.key.site == site)
            and (window is None or cell.key.window == window)
            and (kind is None or cell.key.kind == kind)
        ):
            flags.append("样本含 partial 离线格：缺货时点不明，意愿率为已登记人群的下界口径")

    return WillingnessEstimate(
        label="|".join(_group_key(site, window, kind)),
        eligible_events=len(eligible),
        accepted_events=len(accepted),
        rate=rate,
        flags=flags,
    )


def stockout_risk_matrix(
    cells: dict[CellKey, CellRecord],
) -> list[dict[str, object]]:
    """备餐人员视角的组合缺货风险：站点 × 窗口 × 餐种一行。

    风险用可证实失单率与截断频率表达，并显式带上不可观测格数，避免把离线期当成
    "没有缺货"。每行同时给出需求下界与点估计支持情况。
    """
    rows: list[dict[str, object]] = []
    for site in sorted({c.key.site for c in cells.values()}):
        for window in sorted({c.key.window for c in cells.values()}):
            for kind in sorted({c.key.kind for c in cells.values()}):
                group = [
                    cell
                    for cell in cells.values()
                    if cell.key.site == site
                    and cell.key.window == window
                    and cell.key.kind == kind
                ]
                if not group:
                    continue
                est = aggregate_demand(cells, site, window, kind)
                will = substitution_willingness(cells, site, window, kind)
                proven_lost = sum(
                    len(cell.unmet_registered) for cell in group
                )
                lower_demand = sum(
                    cell_demand_components(cell)["booked_demand"] for cell in group
                )
                rows.append(
                    {
                        "site": site,
                        "window": window,
                        "kind": kind,
                        "days": len(group),
                        "capped_days": est.capped_samples,
                        "unobservable_days": est.unobservable_samples,
                        "demand_lower": lower_demand,
                        "demand_estimate": est.estimate,
                        "estimate_supported": est.supported,
                        "proven_lost": proven_lost,
                        "proven_lost_rate": (
                            proven_lost / lower_demand if lower_demand else 0.0
                        ),
                        "substitution_accepted": will.accepted_events,
                        "substitution_eligible": will.eligible_events,
                        "substitution_rate": will.rate,
                        "flags": est.flags + will.flags,
                    }
                )
    return rows
