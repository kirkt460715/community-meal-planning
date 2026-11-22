# 社区助餐缺货下的需求测算与次日备餐

社区助餐的模拟出票与备餐资料。时段为北京时间，early 先于 late。未成单回执只覆盖确有登记的候补；不声明全体有需求者。原订类别和实际餐种分别记录。任何站点都不能为 restricted 餐替餐。截单后 production 和 delivery 均按已确认批量占用。

资料均为合成记录，采用 UTF-8 编码。交换字段、标识和状态见[资料说明](docs/data.md)。

项目使用 Python 3.11.2，资料查看与检测只依赖标准库。在项目目录运行以下命令，可查看各集合的记录数，并检查字段、重复键与跨表引用。

```sh
python3 inspect_records.py
python3 -m unittest discover -s tests -v
```
