# 社区助餐缺货下的需求测算与次日备餐

社区助餐的模拟出票与备餐资料。时段为北京时间，early 先于 late。未成单回执只覆盖确有登记的候补；不声明全体有需求者。原订类别和实际餐种分别记录。任何站点都不能为 restricted 餐替餐。截单后 production 和 delivery 均按已确认批量占用。

资料均为合成记录，采用 UTF-8 编码。交换字段、标识和状态见[资料说明](docs/data.md)，
测算口径、分单约束与兑现流程见[方法说明](docs/method.md)。

项目使用 Python 3.11.2，资料查看与检测只依赖标准库。在项目目录运行以下命令，可查看各集合的记录数，并检查字段、重复键与跨表引用。

```sh
python3 inspect_records.py
python3 -m unittest discover -s tests -v
```

日常测算、分单与兑现通过 `mealplan` 包入口完成，可用下一天的数据原样重跑：

```sh
python3 -m mealplan.cli check       # 三分类、库存恒等式与菜单/登记约束校验
python3 -m mealplan.cli classify    # 逐库存格的可观测性与需求分解
python3 -m mealplan.cli demand --site S1 --window early --kind soft
python3 -m mealplan.cli risk        # 站点×窗口×餐种组合缺货风险
python3 -m mealplan.cli backtest    # 只用历史可观测格回看，并分解误差来源
python3 -m mealplan.cli plan        # 次日分单（整批、线路、车辆、保底、停做）
python3 -m mealplan.cli fulfill     # 分批确认、停做改约与余量承接
```

关键口径：partial 离线格只给需求下界；候补未出餐才是可证实缺货失单；换餐/换点的
需求只挂原订格，接收格只记库存消耗；restricted 专用餐不可替餐；样本支持不足时
不输出点估计。
