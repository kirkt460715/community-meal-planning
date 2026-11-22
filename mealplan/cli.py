"""负责人日常工作入口：每条命令都可用下一天的数据重跑。

子命令：

- ``check``       数据完整性与业务约束校验（库存恒等式、菜单/登记约束、跨表引用）
- ``classify``    逐库存格列出三类可观测性与需求分解
- ``demand``      按站点/窗口/餐种看需求下界、点估计支持与替换意愿
- ``risk``        组合缺货风险矩阵
- ``backtest``    滚动回看并分解误差来源，列出政策可用替代
- ``plan``        次日分单（含保底、截单后变化、已承诺占用）
- ``fulfill``     分批确认、临时停做改约、余量承接
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import allocation, backtest, demand as demand_mod, fulfillment, records
from .records import load_tables


def _default_data_dir() -> Path:
    return Path(__file__).resolve().parent.parent / "data"


def _print_json(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def cmd_check(args) -> int:
    tables = load_tables(args.data)
    cells, events, missing = records.build_cells(tables)
    obs = {records.OBS_VISIBLE: 0, records.OBS_CAPPED: 0, records.OBS_UNOBSERVABLE: 0}
    for cell in cells.values():
        obs[cell.observation] += 1
    closed_windows = sorted(
        {
            f'{row["day"]} {row["site"]}/{row["window"]}'
            for row in tables["calendar"]
            if row["state"] == "closed"
        }
    )
    summary = {
        "collections": {name: len(rows) for name, rows in sorted(tables.items())},
        "inventory_cells": len(cells),
        "order_events": len(events),
        "observation_counts": obs,
        "closed_windows_no_service": closed_windows,
        "booked_orders_in_closed_windows": len(missing),
        "checks": [
            "库存恒等式（开档-收档=原订实发+换领流入）全部成立",
            "restricted 专用餐无任何替餐/换点事件",
            "换餐/换点事件全部来自 permission=both 登记",
        ],
    }
    _print_json(summary)
    return 0


def cmd_classify(args) -> int:
    tables = load_tables(args.data)
    cells, _, _ = records.build_cells(tables)
    rows = []
    for cell in sorted(cells.values(), key=lambda c: c.key.as_tuple()):
        comp = records.cell_demand_components(cell)
        rows.append(
            {
                "cell": cell.key.as_tuple(),
                "observation": cell.observation,
                "opening": cell.inventory.opening,
                "closing": cell.inventory.closing,
                "evidence": cell.inventory.evidence,
                **comp,
            }
        )
    _print_json(rows)
    return 0


def _parse_group(args) -> tuple[str | None, str | None, str | None]:
    return args.site or None, args.window or None, args.kind or None


def cmd_demand(args) -> int:
    tables = load_tables(args.data)
    cells, _, _ = records.build_cells(tables)
    site, window, kind = _parse_group(args)
    est = demand_mod.aggregate_demand(cells, site, window, kind)
    will = demand_mod.substitution_willingness(cells, site, window, kind)
    _print_json(
        {
            "group": est.label,
            "demand": {
                "lower": est.lower,
                "estimate": est.estimate,
                "estimate_supported": est.supported,
                "visible_samples": est.visible_samples,
                "capped_samples": est.capped_samples,
                "unobservable_samples": est.unobservable_samples,
                "proven_lost": est.proven_lost,
                "substituted": est.substituted,
            },
            "substitution_willingness": {
                "eligible_events": will.eligible_events,
                "accepted_events": will.accepted_events,
                "rate": will.rate,
                "supported": will.supported,
            },
            "flags": est.flags + will.flags,
        }
    )
    return 0


def cmd_risk(args) -> int:
    tables = load_tables(args.data)
    cells, _, _ = records.build_cells(tables)
    _print_json(demand_mod.stockout_risk_matrix(cells))
    return 0


def cmd_backtest(args) -> int:
    tables = load_tables(args.data)
    cells, events, _ = records.build_cells(tables)
    summary = backtest.run_backtest(cells, tables, min_history=args.min_history)
    payload = {
        "error_source_counts": summary.error_source_counts(),
        "mean_forecast_error_on_visible_days": round(summary.mean_forecast_error, 3),
        "censored_shortfall_cells": summary.censored_shortfalls,
        "unobservable_cells": summary.unobservable_cells,
        "market_day_cells": summary.event_day_cells,
        "policy_substitutions": summary.policy_substitutions,
        "blocked_restricted": summary.blocked_restricted,
        "blocked_by_registration": summary.blocked_by_registration,
        "substitution_chains": backtest.trace_substitution_chain(events),
        "cell_results_excerpt": [
            result.__dict__
            for result in summary.results
            if result.error_source != backtest.ERR_FORECAST or result.error != 0
        ][: args.limit],
    }
    _print_json(payload)
    return 0


def _project_demands(tables) -> tuple[dict[tuple[str, str, str], int], str, list[str]]:
    """用历史 visible 格均值为次日生成每个 站点×窗口×餐种的需求份数。

    次日 = 日历最后一天的下一天。只有未截断（visible）格进入均值，向上取整；
    没有足够 visible 历史的组不硬给数字，转由下界与风险矩阵提示，避免拿饱和销量
    冒充需求。返回 ``(需求字典, 次日, 提示)``。
    """
    import math
    from datetime import date, timedelta

    cells, _, _ = records.build_cells(tables)
    last_day = max(row["day"] for row in tables["calendar"])
    target_day = (
        date.fromisoformat(last_day) + timedelta(days=1)
    ).isoformat()
    demands: dict[tuple[str, str, str], int] = {}
    notes: list[str] = []
    for site in records.SITES:
        for window in records.WINDOWS:
            for kind in records.KINDS:
                visible_vals = [
                    records.cell_demand_components(cell)["booked_demand"]
                    for cell in cells.values()
                    if cell.key.site == site
                    and cell.key.window == window
                    and cell.key.kind == kind
                    and cell.observation == records.OBS_VISIBLE
                ]
                if len(visible_vals) < demand_mod.MIN_VISIBLE_SAMPLES:
                    notes.append(
                        f"{site}/{window}/{kind} 可观测样本 {len(visible_vals)} 不足："
                        "不进数字分单，按需求下界与缺货风险人工复核"
                    )
                    continue
                demands[(site, window, kind)] = math.ceil(
                    sum(visible_vals) / len(visible_vals)
                )
    return demands, target_day, notes


def cmd_plan(args) -> int:
    tables = load_tables(args.data)
    demands, target_day, projection_notes = _project_demands(tables)
    plan = allocation.build_plan(tables, demands)
    spare = allocation.remaining_capacity(tables)
    _print_json(
        {
            "target_day": target_day,
            "demands_visible_mean_basis": [
                {"site": s, "window": w, "kind": k, "portions": v}
                for (s, w, k), v in sorted(demands.items())
            ],
            "projection_notes": projection_notes,
            "allocation_lines": [line.__dict__ for line in plan.lines],
            "unmet": plan.unmet,
            "notes": plan.notes,
            "remaining_capacity": spare,
        }
    )
    return 0


def cmd_fulfill(args) -> int:
    tables = load_tables(args.data)
    cells, events, _ = records.build_cells(tables)
    demands, target_day, projection_notes = _project_demands(tables)
    plan = allocation.build_plan(tables, demands)

    # 受临时停做影响的居民：用最近一天对停做餐种仍有登记的人做代表样本，
    # 逐位给出可领餐方式；真实运行时这里接入次日居民登记表。
    _, _, _, changes = allocation._parse_tables(tables)
    drops = [c for c in changes if c.change == "capacity_drop"]
    latest_day = max(cell.key.day for cell in cells.values())
    affected: list[dict[str, str]] = []
    for drop in drops:
        for event in events:
            if event.day != latest_day or event.requested != drop.kind:
                continue
            affected.append(
                {
                    "order_ref": event.order_id,
                    "site": event.booked_site,
                    "window": event.window,
                    "requested_kind": event.requested,
                    "permission": event.permission,
                }
            )

    report = fulfillment.build_fulfillment_report(tables, plan, affected)
    _print_json(
        {
            "target_day": target_day,
            "projection_notes": projection_notes,
            "accepted_commitments": report.accepted_commitments,
            "rejected_confirmations": report.rejected_confirmations,
            "resident_options": [option.__dict__ for option in report.resident_options],
            "late_demand_handling": report.late_demand_handling,
            "notes": report.notes,
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mealplan",
        description="社区助餐缺货下的需求测算、次日分单与兑现追踪",
    )
    parser.add_argument(
        "--data",
        type=Path,
        default=_default_data_dir(),
        help="交换文件目录（默认 data/）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="数据与业务约束校验").set_defaults(func=cmd_check)
    sub.add_parser("classify", help="库存格三分类与需求分解").set_defaults(
        func=cmd_classify
    )

    demand_parser = sub.add_parser("demand", help="需求与替换意愿测算")
    demand_parser.add_argument("--site")
    demand_parser.add_argument("--window", choices=records.WINDOWS)
    demand_parser.add_argument("--kind", choices=records.KINDS)
    demand_parser.set_defaults(func=cmd_demand)

    sub.add_parser("risk", help="组合缺货风险矩阵").set_defaults(func=cmd_risk)

    bt = sub.add_parser("backtest", help="滚动回看与误差分解")
    bt.add_argument("--min-history", type=int, default=5)
    bt.add_argument("--limit", type=int, default=20)
    bt.set_defaults(func=cmd_backtest)

    sub.add_parser("plan", help="次日分单").set_defaults(func=cmd_plan)
    sub.add_parser("fulfill", help="兑现追踪").set_defaults(func=cmd_fulfill)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, KeyError) as error:
        print(f"测算中止：{error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
