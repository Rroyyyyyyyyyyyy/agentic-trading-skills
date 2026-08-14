# 决策 JSON 与行动卡

## 1. 输入原则

金额是 USD 数值。缺字段、重复 JSON 键、NaN/Inf、无时区时间、宣称延迟与实际时间不符都拒绝。`decision_gate.py` 只检查策略与风险，不直连券商；真实操作还必须通过 [execution-contract.md](execution-contract.md) 的两阶段闸门。

完整账号不得出现在输入中。账户字段必须由零参数固定账户快照的脱敏结果转换而来。

```json
{
  "schema_version": "1.0",
  "mode": "decide",
  "as_of_et": "2026-08-13T15:40:00-04:00",
  "data_age_seconds": 120,
  "account": {
    "equity": 5000,
    "cash_available_settled": 1500,
    "broker_buying_power": 1500,
    "unleveraged_buying_power": 1500,
    "account_type": "cash",
    "margin_enabled": false,
    "strategy_external_capital": 5000,
    "peak_equity_adjusted": 5200,
    "positions": [
      {"symbol": "VTI", "value": 2000, "sellable_value": 2000, "asset_class": "etf"}
    ],
    "open_orders": []
  },
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

## 3. 行动表示

- `market_day`：`quantity=null`, `limit_price=null`，使用美元 amount。
- `limit_day`：quantity/limit_price 均为正有限数，`amount = quantity * limit_price` 容差 $0.01。
- symbol 必须是 1-6 位大写英文字母。ETF 和现金等价物必须在 policy 白名单。

这些字段是内部规则化提案，不可直接当作券商请求；必须经 Robinhood 审核回执绑定后才能提交。

## 4. 内部派生

闸门自行计算：净现金流调整回撤、市场/回撤有效股票上限、已有持仓+未成交买单后的暴露、未成交卖单后的可卖额、现金缓冲、日换手和防重键。

`decision_key = sha256(ET date|symbol|side|trigger|amount.2f)` 只用于防止同一行动建议在日内重复生成。

## 5. 人类可读卡

```text
【行动卡 YYYY-MM-DD #N】BUY ABCD $500（市价 DAY，常规时段）
触发：qualified_candidate
闸门：PASS（市场上限 / 回撤上限 / 行动后单股与总暴露）
论点：……
最强反证：……
失效条件：……
wash sale / 财报 / 可交易性：已核对
数据时点：…… ET
券商操作：只在 pre-review、Robinhood review 与 post-review 闸门全部通过时提交。
```

一张卡只对应一个逻辑行动。账户、订单、报价或新闻变化后，旧卡作废重跑。

## 6. 观测成交日志

Skill 可以把另外操作人已完成、且已在只读账户快照中证实的成交记录为 `fill`事件：

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
