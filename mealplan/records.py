"""站点记录的装载、校验与可观测性分类。

业务口径（与 ``docs/data.md`` 及 README 一致）：

1. 一个站点 × 日期 × 时段 × 餐种构成一个**库存格**。库存格只有三种可观测性：

   - ``visible``     收档仍有余量（closing > 0）且全程有记录（covered）：
                     这一格确实有机会但没人把余量买走，销量即真实需求的完整观测。
   - ``unobservable`` evidence 为 partial：开收档之间曾离线，无法解释确切售罄时段，
                     销量是需求的下界，缺口规模不可估计。
   - ``capped``      covered 且收档为 0：售罄时点可确认，销量受供给截断；
                     其中是否还有未满足需求，要结合候补与替代记录判断。

2. 订单按一次需求一条登记（``orders.id`` 唯一）。原订餐种 ``requested`` 与实际餐种
   ``issued`` 分开记录，因此一次原订未兑现、后来改领替代品的需求只能追溯为一个人，
   不能给两种餐各加一人。

3. 菜单约束：任何站点都不能为 ``restricted`` 餐替餐；只有 ``permission == "both"``
   的登记允许换点或换餐，``same_site`` 只允许本站内处理，``exact_only`` 只能领原订。
   只能领专用餐的居民即使主动退单，也不构成替换意愿证据。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import json

KINDS = ("regular", "soft", "restricted")
WINDOWS = ("early", "late")
SITES = ("S1", "S2", "S3")

# 任何站点都不能替餐的餐种
NON_SUBSTITUTABLE_KINDS = frozenset({"restricted"})

# 订单状态
STATE_SERVED = "served"
STATE_SUBSTITUTE = "substitute"
STATE_WAITLIST = "waitlist"

# 登记的选择范围
PERM_BOTH = "both"          # 允许换点或换餐
PERM_SAME_SITE = "same_site"
PERM_EXACT_ONLY = "exact_only"

# 库存完整度
EVIDENCE_COVERED = "covered"
EVIDENCE_PARTIAL = "partial"

# 可观测性分类
OBS_VISIBLE = "visible"            # 有余量、未截断：需求可完整观测
OBS_UNOBSERVABLE = "unobservable"  # 离线过：售罄时点不可知
OBS_CAPPED = "capped"              # 确认售罄：销量被截断


def load_collection(path: str | Path) -> list[dict[str, str]]:
    """读取一个交换集合，返回记录数组（全部值为字符串）。"""
    with Path(path).open(encoding="utf-8") as source:
        content = json.load(source)
    return content["rows"]


def load_tables(data_dir: str | Path) -> dict[str, list[dict[str, str]]]:
    """读取 ``data_dir`` 下全部七张表。"""
    data_dir = Path(data_dir)
    return {
        path.stem: load_collection(path)
        for path in sorted(data_dir.glob("*.json"))
    }


@dataclass(frozen=True)
class CellKey:
    """库存格标识：站点、日期、时段、餐种。"""

    site: str
    day: str
    window: str
    kind: str

    def as_tuple(self) -> tuple[str, str, str, str]:
        return (self.site, self.day, self.window, self.kind)


@dataclass
class OrderEvent:
    """一次居民需求的归并视图，保留原订与实发的完整去向。"""

    order_id: str
    day: str
    booked_site: str
    window: str
    requested: str
    issued: str            # 空串表示未出餐
    served_site: str       # 空串表示未出餐
    state: str
    permission: str

    @property
    def filled(self) -> bool:
        return bool(self.issued and self.served_site)

    @property
    def meal_changed(self) -> bool:
        """换餐（同站或换点后领到不同餐种）。"""
        return self.filled and self.issued != self.requested

    @property
    def site_changed(self) -> bool:
        """换点领餐。"""
        return self.filled and self.served_site != self.booked_site

    @property
    def booked_key(self) -> CellKey:
        return CellKey(self.booked_site, self.day, self.window, self.requested)

    @property
    def served_key(self) -> CellKey | None:
        if not self.filled:
            return None
        return CellKey(self.served_site, self.day, self.window, self.issued)


@dataclass
class InventoryCell:
    """一个库存格的供给记录与可观测性分类。"""

    key: CellKey
    opening: int
    closing: int
    evidence: str

    @property
    def sold(self) -> int:
        """实发份数（换领消耗计入实发站相应餐种）。"""
        return self.opening - self.closing

    @property
    def observation(self) -> str:
        if self.evidence == EVIDENCE_PARTIAL:
            return OBS_UNOBSERVABLE
        if self.closing == 0:
            return OBS_CAPPED
        return OBS_VISIBLE


@dataclass
class CellRecord:
    """库存格与其上的全部需求事件，是测算与回看的最小单位。"""

    inventory: InventoryCell
    booked_events: list[OrderEvent] = field(default_factory=list)
    # 在别处登记、实际消耗本格库存的事件（换点/换餐流入）
    inbound_events: list[OrderEvent] = field(default_factory=list)

    @property
    def key(self) -> CellKey:
        return self.inventory.key

    @property
    def observation(self) -> str:
        return self.inventory.observation

    @property
    def served_original(self) -> list[OrderEvent]:
        """原订即在本格、且按原订餐种在本站兑现的事件。"""
        return [
            e for e in self.booked_events
            if e.state == STATE_SERVED
        ]

    @property
    def unmet_registered(self) -> list[OrderEvent]:
        """本格登记但未出餐的候补事件（可证实的缺货失单，只覆盖确有登记者）。"""
        return [e for e in self.booked_events if not e.filled]

    @property
    def diverted(self) -> list[OrderEvent]:
        """本格登记、原订未在本格兑现而改领替代品（换餐或换点）的事件。"""
        return [
            e for e in self.booked_events
            if e.filled and (e.meal_changed or e.site_changed)
        ]


def build_cells(tables: dict[str, list[dict[str, str]]]):
    """把库存与订单归并为以库存格为单位的记录。

    返回 ``(cells, events, missing_cells)``：

    - ``cells``         键为 :class:`CellKey` 的库存格记录；
    - ``events``        全部 :class:`OrderEvent`，按订单号排序；
    - ``missing_cells`` 日历停业导致没有库存记录的格键（无供给、无需求登记）。
    """
    events = [
        OrderEvent(
            order_id=row["id"],
            day=row["day"],
            booked_site=row["booked_site"],
            window=row["window"],
            requested=row["requested"],
            issued=row["issued"],
            served_site=row["served_site"],
            state=row["state"],
            permission=row["permission"],
        )
        for row in tables["orders"]
    ]
    events.sort(key=lambda e: e.order_id)

    cells: dict[CellKey, CellRecord] = {}
    for row in tables["inventories"]:
        key = CellKey(row["site"], row["day"], row["window"], row["kind"])
        inv = InventoryCell(
            key=key,
            opening=int(row["opening"]),
            closing=int(row["closing"]),
            evidence=row["evidence"],
        )
        cells[key] = CellRecord(inventory=inv)

    missing: set[CellKey] = set()
    for event in events:
        booked = event.booked_key
        if booked not in cells:
            missing.add(booked)
            continue
        cells[booked].booked_events.append(event)
        served = event.served_key
        if served is not None and served != booked and served in cells:
            cells[served].inbound_events.append(event)

    # 校验库存恒等式：开档 - 收档 == 本格原订实发 + 换领流入实发
    for cell in cells.values():
        consumed = len(cell.served_original) + len(cell.inbound_events)
        if consumed != cell.inventory.sold:
            raise ValueError(
                f"库存恒等式不成立：{cell.key.as_tuple()} 实发 {consumed} "
                f"!= 开档-收档 {cell.inventory.sold}"
            )

    # 校验菜单与登记选择约束
    for event in events:
        if event.requested in NON_SUBSTITUTABLE_KINDS and (
            event.meal_changed or event.site_changed
        ):
            raise ValueError(f"专用餐不允许替餐/换点：{event.order_id}")
        if event.meal_changed or event.site_changed:
            if event.permission != PERM_BOTH:
                raise ValueError(
                    f"只有 both 登记允许换点或换餐：{event.order_id}"
                )

    return cells, events, missing


def cell_demand_components(cell: CellRecord) -> dict[str, int]:
    """一个库存格上按业务口径分解的登记需求。

    - ``served_original`` 本格原订按原餐种本站兑现的人数；
    - ``substituted``    本格原订未在本格兑现、改领替代品的人数（需求仍属于本格，
                         只计一次，不在流入格重复计需求）；
    - ``waitlist``       本格登记但未出餐的人数（可证实缺货失单）；
    - ``inbound``        别处登记、流入本格兑现的人数（计入本格库存消耗，不计入本格需求）；
    - ``booked_demand``  本格登记需求总量 = 前三项之和，即每个居民只计一次。
    """
    served = len(cell.served_original)
    substituted = len(cell.diverted)
    waitlist = len(cell.unmet_registered)
    inbound = len(cell.inbound_events)
    return {
        "served_original": served,
        "substituted": substituted,
        "waitlist": waitlist,
        "inbound": inbound,
        "booked_demand": served + substituted + waitlist,
    }
