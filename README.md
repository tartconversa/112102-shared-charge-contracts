# 联合充电站的可比需求与费用承诺

共享充电设施的计量、备援权益和合同迁移回执。所有标识、金额、地名和观察值均为合成数据。

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

## 本地工具

`billing/` 只依赖标准库，读取上述来源表，从不改写它们。更正、人工迁移、结算和迁移分期记录在单独的本地台账 `var/ledger.json`（不进版本库），每次写入生成一个新修订号；任何费用都能用修订号 + 快照摘要重放。

```sh
python -m billing.cli init                     # 用当前来源表建立修订 1
python -m billing.cli validate                 # 表结构、引用、业务规则检查
python -m billing.cli reconcile --station B    # 每个区间是否只有一条有效读数
```

### 分析人员：候选时段的两套合约

```sh
python -m billing.cli compare --station A --window 195:300 --day 1
python -m billing.cli compare --station A --day 5 --json
python -m billing.cli relocate --right R1 --to B --window 225:270
python -m billing.cli curve --station B          # 每个区间的证据，空值给区间
```

- 旧约只计电量；新约计电量 + 需量 + 保障费。两侧使用同一份证据、同一修订。
- 需量取"可证明的峰值"；窗口内有未观测区间时另给上下限带，**空值不按零计，也不编造精确峰值**。
- 保障费按站点**同时**承诺的备用功率计（不是把重叠合同相加）。
- `--day` 是"当天已知"的时点：晚到的更正、第 5 天才到的 `reduced` 在此之前不可见。
- `relocate` 显示把客户挪走后**仍然要承担**的原站保障、目的站共同占用和证据不足；原站需量下降只给区间（不把某台设备的最大值摊给每个租户）。
- 未归属负荷（边界读数减去全部子表）单独列出，不摊给任何客户。
- 超过 `cap_kw` 时按三种去向拆分：未生效方案（重新议价）、已生效承诺（照旧履行）、依赖本站备援的客户（需临时替代）。

### 客户运营：迁移登记与权利核对

```sh
python -m billing.cli customers                # 各客户的备用权利、站点余量
python -m billing.cli migrations               # 当前迁移状态
python -m billing.cli request --right R1 --site B --window 225:270   # 预检，不落账
python -m billing.cli migrate --id M1 --right R1 --to B --minute 240 --signed-kw 16 --day 2
python -m billing.cli accept --id M1 --day 7
python -m billing.cli withdraw --id M1 --day 4 --note "客户暂停"
python -m billing.cli overlap --right R1 --signed-day 4 --handoff-day 6
```

- `offered` 只是报价，原条款继续有效；`partial_signed` 后原承运仍在履行；`handoff_received` 才把已交接容量转新约；`accepted` 才只剩新约。
- 分期迁移的 `signed_kw` 可累加，但不得超过权利本身的 `firm_kw`；未签部分继续按旧约计费，双条款并行成本由 `overlap` 单列。
- 备用确认与签收分开：必须有覆盖整个窗口的有效库存，且同时容纳该站全部重叠承诺；`reduced` 行的余量按实际数字计算，但不能确认超过它的承诺（预检返回 `reduced` / `short` / `partial` / `none`，不返回成功）。
- 同一权利的两个不兼容请求不能同时确认；必须先撤回。

### 财务：从计量与真实承诺还原迁移组合

```sh
python -m billing.cli settle --id S1 --station A --window 195:300 --day 1 --note 基准
python -m billing.cli correct --id X1 --day 5 --set B/main@240=47 --note "补传" --restate
python -m billing.cli restate --id S1
python -m billing.cli settlements
python -m billing.cli revisions                # 每步的修订与快照摘要
```

- `settle` 把分析钉在当时的修订和"当天已知"上，事后仍可按原口径回看。
- 触及已结算区间的更正默认被拒绝；加 `--restate` 才落账，并同时输出更正前后对照。
- 修订快照可重放：`compare --revision N --day D` 与当时结果一致。
- `validate` 会报告来源表在快照之后是否被改动（`source-drift`），提示建立新修订。

## 本地检查

使用 Python 3.14，仅需标准库。

```sh
python -m unittest discover -s tests -v
```
