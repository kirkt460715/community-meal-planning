"""次日分单：在真实厨房批量、配送线路、菜单要求与保底专用餐约定下比较方案。

约束口径（对应 kitchens / delivery / commitments / changes 四张表）：

**生产侧**

- 每条 ``kitchens`` 记录是一种餐在一个厨房的整批生产：批大小 ``batch_size``、
  批数上限 ``max_batches``；同一厨房的餐种共用一条产线，批数总和不超过
  ``line_batches``。分单份数必须按整批落地，不足一批也要占一批。
- ``changes.capacity_drop``（scope=line）表示某厨房临时停做某餐种，对应批数上限
  立即降为 0；已确认批次占用的容量不可移动。

**配送侧**

- 每条 ``delivery`` 线路只服务固定 厨房→站点×窗口，单线路有 ``capacity`` 份数上限；
  同一辆车（vehicle）同一时段所有线路合计不超过 ``vehicle_capacity``。
- 站点某窗口在日历中停业时，不存在可行线路，县里总量再够也不能向该窗口派餐。

**菜单与保底**

- restricted 专用餐不可替餐：``changes.minimum`` 给出的保底份数必须由对应站点窗口
  的 restricted 产能与线路承接，且不能折算成别的餐种。
- 换餐/换点只对 permission=both 的居民生效，分单阶段按原订餐种备餐，替代是履约阶段
  的动作，不在分单里预先合并需求。

**分批确认**

- commitments 中 ``accepted`` 批次真实占用生产批量与车辆份数；``planned`` 尚未占用。
  新分单只能落在剩余容量里，并优先让保底专用餐与已承诺居民落地。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from .records import NON_SUBSTITUTABLE_KINDS


@dataclass
class ProductionOption:
    production_id: str
    kitchen: str
    kind: str
    batch_size: int
    max_batches: int
    line_batches: int


@dataclass
class RouteOption:
    route_id: str
    vehicle: str
    kitchen: str
    site: str
    window: str
    capacity: int
    vehicle_capacity: int


@dataclass
class Commitment:
    commitment_id: str
    route_id: str
    production_id: str
    units: int
    state: str  # accepted | planned
    at: str


@dataclass
class CapacityChange:
    change_id: str
    change: str       # capacity_drop | minimum | late_demand
    scope: str        # line | terminal
    reference: str    # 厨房或 站点-窗口
    kind: str
    portions: int
    at: str


@dataclass
class AllocationLine:
    route_id: str
    production_id: str
    site: str
    window: str
    kitchen: str
    kind: str
    portions: int
    batches: int
    reason: str


@dataclass
class AllocationPlan:
    lines: list[AllocationLine] = field(default_factory=list)
    unmet: list[dict[str, object]] = field(default_factory=list)
    """无法承接的需求条目及原因（产能/线路/车辆/停业窗口）。"""
    notes: list[str] = field(default_factory=list)

    def total_portions(self) -> int:
        return sum(line.portions for line in self.lines)


def _parse_tables(tables: dict[str, list[dict[str, str]]]):
    options = [
        ProductionOption(
            production_id=r["id"],
            kitchen=r["kitchen"],
            kind=r["kind"],
            batch_size=int(r["batch_size"]),
            max_batches=int(r["max_batches"]),
            line_batches=int(r["line_batches"]),
        )
        for r in tables["kitchens"]
    ]
    routes = [
        RouteOption(
            route_id=r["id"],
            vehicle=r["vehicle"],
            kitchen=r["kitchen"],
            site=r["site"],
            window=r["window"],
            capacity=int(r["capacity"]),
            vehicle_capacity=int(r["vehicle_capacity"]),
        )
        for r in tables["delivery"]
    ]
    commitments = [
        Commitment(
            commitment_id=r["id"],
            route_id=r["route"],
            production_id=r["production"],
            units=int(r["units"]),
            state=r["state"],
            at=r["at"],
        )
        for r in tables["commitments"]
    ]
    changes = [
        CapacityChange(
            change_id=r["id"],
            change=r["change"],
            scope=r["scope"],
            reference=r["reference"],
            kind=r["kind"],
            portions=int(r["portions"]),
            at=r["at"],
        )
        for r in tables["changes"]
    ]
    return options, routes, commitments, changes


def _closed_windows(
    tables: dict[str, list[dict[str, str]]], target_day: str
) -> set[tuple[str, str]]:
    """只返回目标日当天停业的 站点×窗口；历史停业不影响次日分单。"""
    return {
        (row["site"], row["window"])
        for row in tables.get("calendar", [])
        if row["state"] == "closed" and row["day"] == target_day
    }


def _target_day(tables: dict[str, list[dict[str, str]]]) -> str:
    days = [row["day"] for row in tables["calendar"]]
    return max(days)


def build_plan(
    tables: dict[str, list[dict[str, str]]],
    demands: dict[tuple[str, str, str], int],
) -> AllocationPlan:
    """按给定的站点×窗口×餐种需求（份数）构造次日分单。

    ``demands`` 的键为 ``(site, window, kind)``。处理顺序刻意固定：

    1. 先落 changes.minimum 的 restricted 保底（不可替餐、不可降级）；
    2. 再落 changes.late_demand 的截单后新增需求；
    3. 最后按 restricted → soft → regular 落常规需求，保证专用餐优先占容量。

    每条可行线路按整批向上取批，占用对应产线批数、线路份数与车辆时段份数；
    已 accepted 的承诺先扣减容量。任何无法落地的条目进 ``unmet`` 并写明卡在哪个约束。
    """
    options, routes, commitments, changes = _parse_tables(tables)
    target_day = _target_day(tables)
    closed = _closed_windows(tables, target_day)
    plan = AllocationPlan()

    # ---- 剩余容量初始化：accepted 承诺先行占用 ----
    line_batches_left = {o.production_id: o.max_batches for o in options}
    kitchen_line_left = {
        (o.kitchen, o.line_batches): o.line_batches for o in options
    }
    route_left = {r.route_id: r.capacity for r in routes}
    vehicle_window_left: dict[tuple[str, str], int] = {}
    for r in routes:
        key = (r.vehicle, r.window)
        vehicle_window_left.setdefault(key, r.vehicle_capacity)

    # 产线容量下调（capacity_drop：临时停做某餐种）
    drops = [c for c in changes if c.change == "capacity_drop" and c.scope == "line"]
    for drop in drops:
        for o in options:
            if o.kitchen == drop.reference and o.kind == drop.kind:
                line_batches_left[o.production_id] = 0
                plan.notes.append(
                    f"{drop.change_id}: {o.kitchen} 临时停做 {o.kind}，"
                    f"{o.production_id} 批数上限降为 0"
                )

    def accept_commitment(commitment: Commitment) -> None:
        route = next(r for r in routes if r.route_id == commitment.route_id)
        option = next(o for o in options if o.production_id == commitment.production_id)
        batches = math.ceil(commitment.units / option.batch_size)
        line_batches_left[option.production_id] -= batches
        kitchen_line_left[(option.kitchen, option.line_batches)] -= batches
        route_left[route.route_id] -= commitment.units
        vehicle_window_left[(route.vehicle, route.window)] -= commitment.units
        plan.lines.append(
            AllocationLine(
                route_id=route.route_id,
                production_id=option.production_id,
                site=route.site,
                window=route.window,
                kitchen=option.kitchen,
                kind=option.kind,
                portions=commitment.units,
                batches=batches,
                reason=f"已 {commitment.state} 承诺 {commitment.commitment_id}",
            )
        )

    for commitment in sorted(commitments, key=lambda c: (c.at, c.commitment_id)):
        if commitment.state == "accepted":
            accept_commitment(commitment)
    planned = [c for c in commitments if c.state != "accepted"]

    # ---- 构造待处理需求队列：保底 → 截单后新增 → 常规（专用餐优先）----
    # 同一 站点×窗口×餐种 的多来源需求先合并，保证一个居民/一份需求只排一次；
    # 保底约束体现为该格的最低落地份数，而不是在常规需求之外再加一遍。
    queue_map: dict[tuple[str, str, str], tuple[int, str]] = {}

    def enqueue(site: str, window: str, kind: str, portions: int, reason: str) -> None:
        key = (site, window, kind)
        if key in queue_map:
            prev_portions, prev_reason = queue_map[key]
            queue_map[key] = (prev_portions + portions, f"{prev_reason}；{reason}")
        else:
            queue_map[key] = (portions, reason)

    minimums = {
        (c.reference, c.kind): c.portions
        for c in changes
        if c.change == "minimum" and c.scope == "terminal"
    }
    for (terminal, kind), portions in sorted(minimums.items()):
        site, window = terminal.split("-", 1)
        enqueue(site, window, kind, portions, "minimum 保底专用餐")
    for c in sorted(changes, key=lambda x: x.at):
        if c.change == "late_demand":
            site, window = c.reference.split("-", 1)
            enqueue(site, window, c.kind, c.portions, f"截单后新增 {c.change_id}")

    kind_rank = {"restricted": 0, "soft": 1, "regular": 2}
    for (site, window, kind), portions in sorted(
        demands.items(), key=lambda item: (kind_rank.get(item[0][2], 9), item[0])
    ):
        enqueue(site, window, kind, portions, "常规需求")

    queue = [
        (site, window, kind, portions, reason)
        for (site, window, kind), (portions, reason) in queue_map.items()
    ]
    queue.sort(key=lambda q: (kind_rank.get(q[2], 9), q[0], q[1]))

    def feasible_routes(site: str, window: str, kind: str):
        """给出能把某厨房某餐种送到该站点窗口的 (线路, 产能选项)，按富余度排序。"""
        out = []
        for route in routes:
            if route.site != site or route.window != window:
                continue
            for option in options:
                if option.kitchen != route.kitchen or option.kind != kind:
                    continue
                out.append((route, option))
        out.sort(
            key=lambda pair: (
                line_batches_left[pair[1].production_id],
                route_left[pair[0].route_id],
            ),
            reverse=True,
        )
        return out

    def try_allocate(
        site: str, window: str, kind: str, portions: int, reason: str
    ) -> int:
        """尽量把 portions 份需求落地，返回实际承接份数。"""
        if (site, window) in closed:
            plan.unmet.append(
                {
                    "site": site,
                    "window": window,
                    "kind": kind,
                    "portions": portions,
                    "reason": "该窗口日历停业，无可行配送线路",
                }
            )
            return 0
        remaining = portions
        for route, option in feasible_routes(site, window, kind):
            if remaining <= 0:
                break
            if line_batches_left[option.production_id] <= 0:
                continue
            room_route = route_left[route.route_id]
            room_vehicle = vehicle_window_left[(route.vehicle, route.window)]
            room_kitchen = kitchen_line_left[(option.kitchen, option.line_batches)]
            if room_route <= 0 or room_vehicle <= 0 or room_kitchen <= 0:
                continue
            # 份数上限取线路、车辆、剩余批数、产线批数的最小值
            max_portions = min(
                remaining,
                room_route,
                room_vehicle,
                line_batches_left[option.production_id] * option.batch_size,
                room_kitchen * option.batch_size,
            )
            if max_portions <= 0:
                continue
            # 整批落地：按上限份数向上取批，但批数不得超过任一剩余批数
            batches = min(
                math.ceil(max_portions / option.batch_size),
                line_batches_left[option.production_id],
                room_kitchen,
            )
            take = min(max_portions, batches * option.batch_size)
            line_batches_left[option.production_id] -= batches
            kitchen_line_left[(option.kitchen, option.line_batches)] -= batches
            route_left[route.route_id] -= take
            vehicle_window_left[(route.vehicle, route.window)] -= take
            plan.lines.append(
                AllocationLine(
                    route_id=route.route_id,
                    production_id=option.production_id,
                    site=site,
                    window=window,
                    kitchen=option.kitchen,
                    kind=kind,
                    portions=take,
                    batches=batches,
                    reason=reason,
                )
            )
            remaining -= take
        if remaining:
            blockers = []
            if not feasible_routes(site, window, kind):
                blockers.append("无匹配厨房/线路")
            if kind in NON_SUBSTITUTABLE_KINDS:
                blockers.append("专用餐不可替餐，缺口不能折算")
            plan.unmet.append(
                {
                    "site": site,
                    "window": window,
                    "kind": kind,
                    "portions": remaining,
                    "reason": "；".join(blockers) or "剩余生产批量或车辆份数不足",
                }
            )
        return portions - remaining

    for site, window, kind, portions, reason in queue:
        try_allocate(site, window, kind, portions, reason)

    # planned 承诺不占容量，但要提示它尚未确认、确认时仍需通过同一套容量检验
    for commitment in planned:
        plan.notes.append(
            f"{commitment.commitment_id} 仍为 planned：未占用容量，确认前需重跑容量检验"
        )

    return plan


def remaining_capacity(tables: dict[str, list[dict[str, str]]]) -> dict[str, object]:
    """返回 accepted 承诺占用后的剩余生产批量、线路与车辆份数，供余量承接决策。"""
    options, routes, commitments, changes = _parse_tables(tables)
    line_batches_left = {o.production_id: o.max_batches for o in options}
    kitchen_line_left = {
        (o.kitchen, o.line_batches): o.line_batches for o in options
    }
    route_left = {r.route_id: r.capacity for r in routes}
    vehicle_window_left: dict[tuple[str, str], int] = {}
    for r in routes:
        vehicle_window_left.setdefault((r.vehicle, r.window), r.vehicle_capacity)

    for drop in (c for c in changes if c.change == "capacity_drop"):
        for o in options:
            if o.kitchen == drop.reference and o.kind == drop.kind:
                line_batches_left[o.production_id] = 0

    for commitment in commitments:
        if commitment.state != "accepted":
            continue
        route = next(r for r in routes if r.route_id == commitment.route_id)
        option = next(o for o in options if o.production_id == commitment.production_id)
        batches = math.ceil(commitment.units / option.batch_size)
        line_batches_left[option.production_id] -= batches
        kitchen_line_left[(option.kitchen, option.line_batches)] -= batches
        route_left[route.route_id] -= commitment.units
        vehicle_window_left[(route.vehicle, route.window)] -= commitment.units

    return {
        "production_batches_left": line_batches_left,
        "kitchen_line_batches_left": {
            f"{k[0]}|line{k[1]}": v for k, v in kitchen_line_left.items()
        },
        "route_portions_left": route_left,
        "vehicle_window_portions_left": {
            f"{k[0]}|{k[1]}": v for k, v in vehicle_window_left.items()
        },
    }
