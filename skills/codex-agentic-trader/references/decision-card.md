# 决策契约与决策卡格式

## 1. 决策 JSON 契约（`decision_gate.py --input` 的输入）

所有金额为 USD 数值；缺字段、非有限数、类型错误一律拒绝。`mode` 必须显式为 `decide`。

```json
{
  "schema_version": "1.0",
  "mode": "decide",
  "as_of_et": "2026-08-13T15:40:00-04:00",
  "data_age_seconds": 120,
  "account": {
    "equity": 5000.0,
    "cash_available_settled": 800.0,
    "peak_equity_adjusted": 5200.0,
    "positions": [
      {"symbol": "VTI", "value": 2200.0, "asset_class": "etf"},
      {"symbol": "XYZ", "value": 700.0, "asset_class": "stock"}
    ]
  },
  "market_state": {
    "benchmark_a_pass": true,
    "benchmark_b_pass": false
  },
  "history": {
    "orders_today": 1,
    "turnover_today": 300.0,
    "used_decision_keys": ["<sha256>"]
  },
  "proposal": {
    "symbol": "ABCD",
    "side": "buy",
    "amount": 500.0,
    "asset_class": "stock",
    "trigger": "qualified_candidate",
    "order_type": "market_day",
    "thesis": "一句话论点",
    "counter_thesis": "一句话反论点",
    "invalidation": "失效条件（价格/时间/消息）",
    "declares": {
      "price": 25.0,
      "market_cap": 20000000000,
      "avg_daily_volume": 2000000,
      "above_50dma": true,
      "above_200dma": true,
      "ret_3m_positive": true,
      "ret_6m_positive": true,
      "rel_strength_vs_benchmark_20d": true,
      "days_to_earnings": 10,
      "no_thesis_breaking_news": true,
      "is_leveraged_or_inverse": false,
      "is_otc": false
    }
  }
}
```

要点：

- `market_state`：两个基准 ETF（policy 中定义，默认 VTI/QQQM）是否通过"6 个月回报为正且收盘站上 200 日均线"，**按完整日收盘确认**，盘中数据不算。
- `trigger` 合法值：`weight_deviation`（权重偏离≥阈值）、`market_state_change`、`drawdown_action`、`thesis_break`、`qualified_candidate`。其他值拒绝。
- `declares` 在 `side=buy` 且 `asset_class=stock` 时全字段必填；ETF 买入只需 `is_leveraged_or_inverse`/`is_otc`；卖出只需资产类字段。声明数据必须来自新鲜行情，AI 对声明的真实性负责——**闸门校验的是逻辑，数据造假 = 闸门失效**，这是纪律问题不是技术问题。
- `decision_key = sha256(ET日期|symbol|side|trigger|amount取两位小数)`，由闸门内部计算并查重。注意：这是**防手滑不防对抗**的记账级去重（改一分钱即新 key），真正的重复交易防线是对账纪律（record 未完成不出新执行卡）。
- **符号白名单**：`asset_class=etf` 的 symbol 必须在 policy `etf_symbols` 内（默认 VTI/QQQM）、`cash_equiv` 必须在 `cash_equiv_symbols` 内（默认 SGOV）——防止把个股标成 ETF/现金等价物绕过准入与集中度门。新增 ETF 需 Roy 改 policy。
- **卖出校验**：必须实际持有同 symbol 且同 asset_class 的仓位，金额不超过持仓市值（禁止裸卖空与跨类卖出）。
- 其他硬校验：金额 ≥ policy `min_order_amount`；输入 JSON 出现重复键直接拒绝；`declares` 必须是对象。

## 2. 闸门输出

```json
{
  "would_allow": true,
  "decision_key": "<sha256>",
  "violations": [],
  "derived": {
    "stock_cap_market_state": 0.6,
    "stock_cap_drawdown": 0.7,
    "stock_cap_effective": 0.6,
    "drawdown": 0.038,
    "single_stock_weight_after": 0.14,
    "stocks_total_weight_after": 0.24
  },
  "manual_card": { "...": "见下节字段" },
  "execution_capability": "none"
}
```

`would_allow=false` 时 `violations` 列出全部违规码；会话必须原样呈现，不得筛选或弱化。

## 3. 给 Roy 的决策卡（会话最终输出格式）

```
【决策卡 YYYY-MM-DD #N】BUY ABCD $500（市价 DAY 单，常规时段）
触发器：qualified_candidate ｜ 闸门：PASS（有效股票上限 60%，买后个股 14%/合计 24%）
论点：……
反论点：……
失效条件：跌破 $23.5 收盘 / 财报前 2 日 / 出现XX消息 —— 触发任一则本卡作废
数据时点：2026-08-13 15:40 ET（行情 2 分钟前）
执行：由你在券商 App 手动下单；执行或放弃后回报我记账。
（本卡为规则化研究产物，非投资建议）
```

要求：一张卡只含一笔操作；金额/股数二选一说清楚；失效条件必须可客观判定；卡出后账户状态发生实质变化（新成交、大幅波动）即作废重跑。

## 4. 成交回报 JSON（`journal_append.py --input`，mode=record 用）

```json
{
  "type": "fill",
  "date_et": "2026-08-13",
  "symbol": "ABCD",
  "side": "buy",
  "amount": 500.0,
  "price": 25.1,
  "decision_key": "<对应决策卡的key>",
  "deviation": "none | 描述与卡的偏离",
  "pnl": null,
  "holding_days": null,
  "run_up_5d_pct": 4.2
}
```

卖出成交尽量补 `pnl`（相对成本）与 `holding_days`，供行为画像使用；没有就填 null，不编造。
