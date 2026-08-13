# 决策 JSON 与决策卡

## 1. 输入原则

金额是 USD 数值。缺字段、重复 JSON 键、NaN/Inf、无时区时间、宣称延迟与实际时间不符都拒绝。`decision_gate.py` 只产出研究卡，`execution_capability` 恒为 `none`。

完整账户/挂单字段见 [execution-contract.md](execution-contract.md)。核心 proposal：

```json
{
  "schema_version": "1.0",
  "mode": "decide",
  "as_of_et": "2026-08-13T15:40:00-04:00",
  "data_age_seconds": 120,
  "account": {"...": "see execution-contract.md"},
  "market_state": {
    "benchmark_a_pass": true,
    "benchmark_b_pass": true
  },
  "history": {
    "orders_today": 1,
    "turnover_today": 300,
    "used_decision_keys": []
  },
  "proposal": {
    "symbol": "ABCD",
    "side": "buy",
    "amount": 500,
    "quantity": null,
    "limit_price": null,
    "asset_class": "stock",
    "trigger": "qualified_candidate",
    "order_type": "market_day",
    "thesis": "observable evidence",
    "counter_thesis": "strongest disconfirming case",
    "invalidation": "objective price/time/event condition",
    "declares": {
      "price": 25,
      "market_cap": 20000000000,
      "avg_daily_volume": 2000000,
      "above_50dma": true,
      "above_200dma": true,
      "ret_3m_positive": true,
      "ret_6m_positive": true,
      "rel_strength_vs_benchmark_20d": true,
      "days_to_earnings": 10,
      "no_thesis_breaking_news": true,
      "is_us_listed_common_stock": true,
      "tradable_for_account": true,
      "is_leveraged_or_inverse": false,
      "is_otc": false,
      "wash_sale_conflict": false
    }
  }
}
```

## 2. 触发语义

- `qualified_candidate`：只能是买入普通股，且全部准入字段为真。
- `weight_deviation`：`declares.weight_deviation_pct` 的绝对值必须至少 policy 阈值（默认 0.03）。
- `market_state_change`：只用完成收盘日线确认后的状态变化。
- `drawdown_action`：组合回撤降风险。
- `thesis_break`：个股卖出必须附 `exit_signal`，只允许 policy 中的客观退出信号。

任何买入都必须明确 `wash_sale_conflict=false`；不可确认就是拒绝，不是 false。

## 3. 订单表示

- `market_day`：`quantity=null`, `limit_price=null`，使用美元 amount。
- `limit_day`：quantity/limit_price 均为正有限数，`amount = quantity * limit_price` 容差 $0.01。
- symbol 必须是 1-6 位大写英文字母。ETF 和现金等价物必须在 policy 白名单。

## 4. 内部派生

闸门自行计算：净现金流调整回撤、市场/回撤有效股票上限、已有持仓+未成交买单后的暴露、未成交卖单后的可卖额、现金缓冲、日换手和幂等键。

`decision_key = sha256(ET date|symbol|side|trigger|amount.2f)` 仅是本地防重；不是券商幂等键。真实提交用 `ref_id` UUID，而后续对账用 Robinhood `order.id`。

## 5. 人类可读卡

```text
【决策卡 YYYY-MM-DD #N】BUY ABCD $500（市价 DAY，常规时段）
触发：qualified_candidate
闸门：PASS（市场上限 / 回撤上限 / 买后单股与总暴露）
论点：……
最强反证：……
失效条件：……
wash sale / 财报 / 可交易性：已核对
数据时点：…… ET
执行：shadow 手动，或 supervised review 后逐笔确认。
```

一张卡只对应一个逻辑订单。账户、订单、报价、新闻或 review 回执变化后，旧卡作废重跑。

## 6. 日志 fill 事件

```json
{
  "type": "fill",
  "date_et": "2026-08-13",
  "symbol": "ABCD",
  "side": "buy",
  "amount": 500,
  "price": 25.1,
  "decision_key": "<sha256>",
  "order_id_masked_or_hash": "<non-account identifier>",
  "deviation": "none",
  "pnl": null,
  "holding_days": null
}
```

缺失数据填 null 或省略，不编造。
