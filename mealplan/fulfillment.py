"""分单之后的兑现追踪：分批确认、临时停做、改约与余量承接。

兑现阶段的规则：

1. 批次状态机：``planned → accepted → closed``。只有 accepted 占用真实生产批量与
   车辆份数；确认动作必须通过当前剩余容量检验，容量不足时拒绝确认并说明卡点，
   而不是悄悄超排。

2. 临时停做一种餐（capacity_drop）时：

   - 已 accepted 的承诺不可移动，对应居民必须看到仍可怎样领餐；
   - restricted 专用餐没有替餐选项，受影响居民一律进入"需要联系改约"，不能改发别的餐；
   - 其他餐种、且登记 permission=both 的居民，可在同窗口有余量的站点换点，或在本站
     改领允许的替代餐种；same_site/exact_only 登记同样进入联系改约。
   - 两处同时缺货时，同一批愿意换点的居民只能被承接一次：承接以订单号去重，接收点
     不会把他们记成自己的新增需求。

3. 截单后收到的需求（late_demand）只能用确认后仍剩余的产能与车辆份数承接，走与
   分单相同的整批/线路/车辆约束；承接不了的明确列为待协调。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .allocation import (
    AllocationPlan,
    Commitment,
    RouteOption,
    _parse_tables,
    build_plan,
)
from .records import NON_SUBSTITUTABLE_KINDS, PERM_BOTH, PERM_EXACT_ONLY


@dataclass
class ResidentOption:
    order_ref: str
    site: str
    window: str
    requested_kind: str
    permission: str
    status: str
    """one of: honored | substitute_meal | relocate | contact_reschedule"""
    detail: str


@dataclass
class FulfillmentReport:
    accepted_commitments: list[str]
    rejected_confirmations: list[dict[str, str]]
    resident_options: list[ResidentOption]
    late_demand_handling: list[dict[str, object]]
    notes: list[str] = field(default_factory=list)


def confirm_commitment(
    tables: dict[str, list[dict[str, str]]],
    commitment_id: str,
) -> tuple[bool, str]:
    """尝试把一条 planned 承诺确认为 accepted。

    确认前用当前全部 accepted 占用后的剩余容量复算；通过则返回 ``(True, 说明)``，
    不通过返回 ``(False, 卡点)``，调用方不得改写原始交换表之外的状态。
    """
    _, routes, commitments, changes = _parse_tables(tables)
    target = next((c for c in commitments if c.commitment_id == commitment_id), None)
    if target is None:
        return False, f"承诺 {commitment_id} 不存在"
    if target.state == "accepted":
        return True, f"{commitment_id} 已是 accepted，容量早已占用"

    # 用不含目标承诺的现状构造分单，再尝试把目标份数塞进对应线路/产能
    options_by_id = {row["id"]: row for row in tables["kitchens"]}
    route = next(r for r in routes if r.route_id == target.route_id)
    prod_row = options_by_id[target.production_id]
    if any(
        c.change == "capacity_drop"
        and c.scope == "line"
        and c.reference == route.kitchen
        and c.kind == prod_row["kind"]
        for c in changes
    ):
        return False, f"{route.kitchen} 已临时停做 {prod_row['kind']}，无法确认新批次"

    trial_demands = {
        (route.site, route.window, prod_row["kind"]): target.units
    }
    trial_tables = {
        **tables,
        "commitments": [
            dict(r) for r in tables["commitments"] if r["id"] != commitment_id
        ],
    }
    plan = build_plan(trial_tables, trial_demands)
    landed = sum(
        line.portions
        for line in plan.lines
        if line.site == route.site
        and line.window == route.window
        and line.kind == prod_row["kind"]
        and not line.reason.startswith("已")
    )
    if landed >= target.units:
        return True, f"容量检验通过，可确认 {commitment_id}（{target.units} 份）"
    blockers = "; ".join(item["reason"] for item in plan.unmet) or "容量不足"
    return False, f"确认被拒：{blockers}"


def handle_stop_and_residents(
    tables: dict[str, list[dict[str, str]]],
    plan: AllocationPlan,
    affected_residents: list[dict[str, str]],
) -> list[ResidentOption]:
    """临时停做后，逐位居民给出仍可领餐的方式或联系改约结论。

    ``affected_residents`` 每条至少含 ``order_ref/site/window/requested_kind/permission``。
    同一订单号只处理一次（去重），杜绝两处缺货时重复计数。
    """
    options, routes, commitments, changes = _parse_tables(tables)
    drops = [c for c in changes if c.change == "capacity_drop"]
    # 停做的 厨房×餐种
    dropped = {(c.reference, c.kind) for c in drops}

    # 按站点窗口汇总分单已落地的可领余量（排除被停做产线本身）
    landed: dict[tuple[str, str, str], int] = {}
    for line in plan.lines:
        if (line.kitchen, line.kind) in dropped:
            continue
        key = (line.site, line.window, line.kind)
        landed[key] = landed.get(key, 0) + line.portions

    # 同窗口可送的站点（有线路且未停业）
    closed = {
        (row["site"], row["window"])
        for row in tables.get("calendar", [])
        if row["state"] == "closed"
    }
    routes_by_window: dict[str, list[RouteOption]] = {}
    for route in routes:
        routes_by_window.setdefault(route.window, []).append(route)

    report: list[ResidentOption] = []
    seen: set[str] = set()
    for resident in affected_residents:
        ref = resident["order_ref"]
        if ref in seen:
            continue
        seen.add(ref)
        site = resident["site"]
        window = resident["window"]
        kind = resident["requested_kind"]
        permission = resident["permission"]
        base = ResidentOption(
            order_ref=ref,
            site=site,
            window=window,
            requested_kind=kind,
            permission=permission,
            status="contact_reschedule",
            detail="",
        )

        if kind in NON_SUBSTITUTABLE_KINDS:
            base.detail = "专用餐不可替餐：该餐种停做后只能联系改约其他时段/日期"
            report.append(base)
            continue

        if permission != PERM_BOTH:
            base.detail = (
                "登记选择不允许换点/换餐"
                + ("（exact_only 仅领原订）" if permission == PERM_EXACT_ONLY else "")
                + "，需联系改约"
            )
            report.append(base)
            continue

        # 允许替代：先看本站其他可替餐种余量，再看同窗口其他站点
        alt_kind = "regular" if kind == "soft" else "soft"
        if landed.get((site, window, alt_kind), 0) > 0:
            landed[(site, window, alt_kind)] -= 1
            base.status = "substitute_meal"
            base.detail = f"本站 {window} 改领 {alt_kind}，原订单 {ref} 仅计一次需求"
            report.append(base)
            continue

        relocated = False
        for route in routes_by_window.get(window, []):
            if route.site == site or (route.site, window) in closed:
                continue
            if landed.get((route.site, window, kind), 0) > 0:
                landed[(route.site, window, kind)] -= 1
                base.status = "relocate"
                base.detail = (
                    f"换点至 {route.site} 领 {kind}；接收点只记库存消耗，"
                    f"不新增需求人（订单 {ref}）"
                )
                report.append(base)
                relocated = True
                break
        if not relocated:
            base.detail = "允许替代但同窗口无站点/餐种有余量，需联系改约"
            report.append(base)

    return report


def handle_late_demand(
    tables: dict[str, list[dict[str, str]]],
    plan: AllocationPlan,
) -> list[dict[str, object]]:
    """截单后需求（changes.late_demand）只能用剩余余量承接。"""
    _, _, _, changes = _parse_tables(tables)
    # 已落地分单先占用，再在剩余容量里跑 late_demand
    occupied: dict[tuple[str, str, str], int] = {}
    for line in plan.lines:
        key = (line.site, line.window, line.kind)
        occupied[key] = occupied.get(key, 0) + line.portions

    results: list[dict[str, object]] = []
    for change in changes:
        if change.change != "late_demand":
            continue
        site, window = change.reference.split("-", 1)
        key = (site, window, change.kind)
        # 用完整分单引擎在“既有占用”表上复算：把既有占用伪装成 accepted 承诺
        extra_tables = _tables_with_occupancy(tables, occupied)
        sub_plan = build_plan(extra_tables, {key: change.portions})
        landed = sum(
            line.portions
            for line in sub_plan.lines
            if (line.site, line.window, line.kind) == key
            and line.reason.startswith("常规需求")
        )
        results.append(
            {
                "change_id": change.change_id,
                "site": site,
                "window": window,
                "kind": change.kind,
                "requested": change.portions,
                "accepted_with_spare": landed,
                "unmet": change.portions - landed,
                "blockers": [item["reason"] for item in sub_plan.unmet],
            }
        )
    return results


def _tables_with_occupancy(
    tables: dict[str, list[dict[str, str]]],
    occupied: dict[tuple[str, str, str], int],
) -> dict[str, list[dict[str, str]]]:
    """把计划占用转成临时 accepted 承诺行，供余量复算使用（不改写原始表）。"""
    routes_by_site_window: dict[tuple[str, str], list[dict[str, str]]] = {}
    for row in tables["delivery"]:
        routes_by_site_window.setdefault((row["site"], row["window"]), []).append(row)
    prods_by_kitchen_kind = {
        (row["kitchen"], row["kind"]): row["id"] for row in tables["kitchens"]
    }

    synthetic = [dict(r) for r in tables["commitments"]]
    for i, ((site, window, kind), portions) in enumerate(sorted(occupied.items())):
        candidates = routes_by_site_window.get((site, window), [])
        # 选一条能供该餐种的线路（厨房必须有对应餐种产能）
        route_id = production_id = None
        for route in candidates:
            pid = prods_by_kitchen_kind.get((route["kitchen"], kind))
            if pid is not None:
                route_id, production_id = route["id"], pid
                break
        if route_id is None or production_id is None:
            continue
        synthetic.append(
            {
                "id": f"SYN-{i}",
                "route": route_id,
                "production": production_id,
                "units": str(portions),
                "state": "accepted",
                "at": "synthetic",
            }
        )
    return {**tables, "commitments": synthetic}


def build_fulfillment_report(
    tables: dict[str, list[dict[str, str]]],
    plan: AllocationPlan,
    affected_residents: list[dict[str, str]],
) -> FulfillmentReport:
    """汇总兑现报告：确认结果、居民去向、截单后需求承接。"""
    _, _, commitments, _ = _parse_tables(tables)
    accepted = [c.commitment_id for c in commitments if c.state == "accepted"]
    rejected: list[dict[str, str]] = []
    for commitment in commitments:
        if commitment.state != "accepted":
            ok, detail = confirm_commitment(tables, commitment.commitment_id)
            if not ok:
                rejected.append({"commitment": commitment.commitment_id, "reason": detail})

    options_report = handle_stop_and_residents(tables, plan, affected_residents)
    late = handle_late_demand(tables, plan)
    notes = list(plan.notes)
    return FulfillmentReport(
        accepted_commitments=accepted,
        rejected_confirmations=rejected,
        resident_options=options_report,
        late_demand_handling=late,
        notes=notes,
    )
