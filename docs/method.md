# 测算、分单与兑现方法说明

本说明对应 `mealplan/` 包，入口为 `python3 -m mealplan.cli <命令>`。全部逻辑只依赖
Python 标准库，可用下一天的数据原样重跑。字段与交换表含义见 [资料说明](data.md)。

## 1. 三类记录的区分

站点 × 日期 × 时段 × 餐种构成一个**库存格**，每格只可能属于三类之一：

| 类别 | 判定 | 需求含义 |
| --- | --- | --- |
| `visible`（有机会没人买） | `evidence=covered` 且 `closing>0` | 未售罄，登记需求即真实需求的完整观测；余量是被错过的可售机会，不是缺口 |
| `unobservable`（无法观察需求） | `evidence=partial` | 开收档之间离线，售罄时点不可知；销量与登记需求都只是下界，禁止补数 |
| `capped`（供给截断） | `evidence=covered` 且 `closing=0` | 确认售罄；候补未出餐是**可证实缺货失单**，无候补时只能给需求下界 |

本批数据 747 个库存格中：visible 201、capped 528、unobservable 18（均为 soft 餐、
集中在 3 个日期）。`python3 -m mealplan.cli classify` 逐格给出分类与需求分解。

## 2. 需求只计一次的追溯规则

- 订单表一次需求一行，原订餐种 `requested` 与实发餐种 `issued` 分开记录。
- `substitute` 事件（换餐或换点）的需求只挂在**原订格**：原订格记 1 个登记需求和
  1 条替换意愿，接收格只增加 `inbound` 库存消耗，不再新增需求人。
- 两处同时缺货时，换点居民按订单号去重，不能在两个站点各算一次新增需求。
- `restricted` 专用餐在任何站点都不能替餐；该餐种的候补/退单只计入"制度阻断"，
  不进入替换意愿样本。非 `both` 登记（`same_site`/`exact_only`）同理。
- 库存恒等式 `opening - closing = 原订实发 + 换领流入` 在 `build_cells` 中逐格校验，
  不成立即中止测算。

## 3. 需求量与替换意愿

`python3 -m mealplan.cli demand [--site --window --kind]`：

- **需求下界**：各格登记需求（served + substitute + waitlist）合计，每个居民一次。
- **点估计**：只取组内 visible 格的均值按天数外推；capped/partial 的销量永不进入
  均值，避免把"饱和时卖出的品种"当成需求水平。点估计永远不低于证据下界。
- **样本支持**：组内 visible 格少于 5 个时不出点估计，只给下界并写明原因。
- **替换意愿率**：分母是原订格缺货（capped/partial）且 `permission=both` 的登记，
  分子是其中实际换餐/换点的事件；没有合格样本时显式返回"不可估计"。

`risk` 命令输出站点 × 窗口 × 餐种的组合缺货风险矩阵，带截断天数、离线天数、
可证实失单率与替换意愿，供备餐人员按组合查看。

## 4. 回看检验与误差分解

`python3 -m mealplan.cli backtest` 按时间滚动：对每一天只用此前的 visible 格生成
建议（历史不足 5 格时明确不给数字），再按当天可观测性给误差定性：

- `forecast_error`：visible 天上的纯预测偏差，可用于评价模型；
- `sales_censored_error`：当天售罄，真实需求不可知，负误差只说明未覆盖已暴露需求，
  即"误差来自实销量受限"，不记为模型欠估；
- `unobservable_error`：partial 离线天，售罄时点不可知，不据此评价模型；
- 集市日（`calendar.event=market`）单独标注，活动冲击不与普通日混算。

替代链追溯（`substitution_chains`）逐条列出原订格 → 实发格，并注明接收格不新增
需求人。政策阻断拆成 `blocked_restricted`（制度不可替）与
`blocked_by_registration`（居民登记选择不允许）。

## 5. 次日分单的约束与优先级

`python3 -m mealplan.cli plan` 的目标日为日历最后一天的次日。分单在下列硬约束下
求解，并保证全部约束不被突破（测试逐批、逐线路、逐车辆校验）：

- **生产**：按 `batch_size` 整批落地；单餐种批数不超过 `max_batches`；同厨房共用
  产线，批数合计不超过 `line_batches`；`capacity_drop` 把对应餐种批数上限降为 0。
- **配送**：线路只服务固定 厨房→站点×窗口，受 `capacity` 限制；同车同时段合计不
  超过 `vehicle_capacity`。停业窗口按**目标日当天**日历判断，历史停业不影响次日。
- **菜单与保底**：restricted 缺口不可折算成其他餐种；`minimum` 保底与常规需求先
  合并再排队，同一格需求不重复计数。
- **已承诺占用**：`accepted` 承诺先扣减批量与车辆份数；`planned` 不占容量，确认前
  必须重跑容量检验。

处理顺序：保底专用餐 → 截单后新增（late_demand）→ 常规需求（restricted 优先）。
落不下去的条目进 `unmet` 并写明卡在产能、线路、车辆还是停业——即使县总量够，
某餐种错过对应时段无人能送时，方案同样不可采用。剩余容量由
`remaining_capacity` 单独列出。

## 6. 兑现追踪

`python3 -m mealplan.cli fulfill`：

- **分批确认**：`confirm_commitment` 在当前 accepted 占用之上复算容量，通过才允许
  确认，拒绝时给出具体卡点（含停做产线）。
- **临时停做后的居民去向**：restricted 一律联系改约；`both` 登记优先本站改领允许
  餐种，其次同窗口有余量的站点换点；`same_site`/`exact_only` 联系改约。同一订单号
  只处理一次。
- **截单后需求**：`late_demand` 只允许使用确认后的剩余产能与车辆份数，承接量与
  未承接量分别列出，restricted 承接不了时标注政策原因。

## 7. 命令一览

```sh
python3 -m mealplan.cli check       # 数据与业务约束校验
python3 -m mealplan.cli classify    # 库存格三分类与需求分解
python3 -m mealplan.cli demand --site S1 --window early --kind soft
python3 -m mealplan.cli risk        # 组合缺货风险矩阵
python3 -m mealplan.cli backtest    # 滚动回看与误差分解
python3 -m mealplan.cli plan        # 次日分单
python3 -m mealplan.cli fulfill     # 确认、改约与余量承接
```

所有输出为 UTF-8 JSON；样本不足、离线、截断、政策阻断等情况均以 `flags`/`notes`
明示，不产生无证据的精确数字。
