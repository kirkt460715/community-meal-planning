# 资料说明

各集合保存在 `data/`，JSON 对象的 `fields` 为字段列表，`key` 为集合主键，`rows` 为记录数组。各记录值均为字符串，空串含义以各集合的说明为准。

## calendar

开业、停业和提前可知的集市日。

主键为 `id`，字段为 `id`、`site`、`day`、`window`、`state`、`event`。

## inventories

开档可售份数、收档系统余额、完整度。partial 表示开收档之间曾离线，无法解释确切售罄时段。换领消耗实发站相应餐种的份数。

主键为 `id`，字段为 `id`、`site`、`day`、`window`、`kind`、`opening`、`closing`、`evidence`。

## orders

同一次需求的原订、替代和实际出餐去处。

主键为 `id`，字段为 `id`、`day`、`booked_site`、`window`、`requested`、`issued`、`served_site`、`state`、`permission`。

## kitchens

次日按厨房和餐种可做的整批大小与上限。上限为批数，切换餐种共用同一条产线，总批数上限按最后一列。

主键为 `id`，字段为 `id`、`kitchen`、`kind`、`batch_size`、`max_batches`、`line_batches`。

## delivery

配送车、可行线路、收餐点和份数上限。车同一时段总上限按 vehicle_capacity。

主键为 `id`，字段为 `id`、`vehicle`、`kitchen`、`site`、`window`、`capacity`、`vehicle_capacity`。

## commitments

发给厨房的次日分单。planned 尚未确认；accepted 消耗真实批次与车辆份数。

主键为 `id`，字段为 `id`、`route`、`production`、`units`、`state`、`at`。

## changes

截单后接到的次日变化与不可让步的餐种保障。terminal 档口或 line 产线为影响对象。

主键为 `id`，字段为 `id`、`change`、`scope`、`reference`、`kind`、`portions`、`at`。
