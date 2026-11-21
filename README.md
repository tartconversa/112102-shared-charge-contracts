# 联合充电站的可比需求与费用承诺

共享充电设施的计量、备援权益和合同迁移回执。 所有标识、金额、地名和观察值均为合成数据。

## 来源资料

- `sources/meter_readings.csv`
- `sources/meter_roles.csv`
- `sources/tariffs.csv`
- `sources/guarantees.csv`
- `sources/reserve_inventory.csv`
- `sources/transition_events.csv`

timestamp_minute 是同一结算日的区间起始分钟；kw 为该 15 分钟区间平均有功功率。空值未观测，零为实测无功耗。缓冲设备的正负值各为向设备充电与向站内供电，主表是站点供电边界；子表和缓冲读数都不能另算一份总量。observed_day 是记账方取得资料的相对日。

费用定义见 tariffs。需量是合同区间内的同时功率，电量以功率乘实际观察时间表示，保障费按合同锁定容量计。reserve_inventory 记录能担保接受的备用功率，范围包含起始分钟并不包含终止分钟。后来收到的 reduced 是同片段的新证据。

guarantees 是生效中的原始客户权利。transition_events 的 partial_signed 是客户签了部分迁移，原承运约定的交接以 handoff_received 为准。迁移的备用容量在台账上仍应对已签协议单独核验。

## 本地检查

使用 Python 3.14，仅需标准库。在项目目录执行以下命令检查输入表结构、事件引用和基础记录覆盖。

```sh
python -m unittest discover -s tests -v
```
