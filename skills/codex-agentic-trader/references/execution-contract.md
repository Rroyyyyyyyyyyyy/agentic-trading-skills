# Robinhood 执行契约（2026-08-13）

本契约与当前 Robinhood MCP 的 `review_equity_order` / `place_equity_order` / `get_equity_orders` / `cancel_equity_order` 对齐。工具契约发生变化时必须 fail-closed，不得靠字段猜测兼容。

## 1. 三段流程

```text
fresh account + positions + orders + fills + market snapshot
  -> phase=pre_review -> live_gate
  -> can_review=true -> review_equity_order
  -> refresh all mutable state
  -> phase=post_review -> live_gate binds actual request/response
  -> can_request_confirmation=true
  -> show review + verbatim market_data_disclosure to user
  -> explicit per-order confirmation
  -> place_equity_order with the bound arguments and ref_id
  -> get_equity_orders(order_id=...) until terminal
```

`can_submit` 在闸门输出中恒为 false，因为最后授权发生在用户看到 review 之后，不应由一个事前 JSON 布尔值代替。

## 2. 决策输入

基础字段见 [decision-card.md](decision-card.md)。执行层另要求：

```json
{
  "schema_version": "1.0",
  "phase": "pre_review",
  "as_of_et": "2026-08-13T15:40:00-04:00",
  "data_age_seconds": 120,
  "account": {
    "account_type": "cash",
    "margin_enabled": false,
    "strategy_external_capital": 5000,
    "equity": 5000,
    "cash_available_settled": 1500,
    "broker_buying_power": 1500,
    "unleveraged_buying_power": 1500,
    "peak_equity_adjusted": 5200,
    "positions": [
      {"symbol": "VTI", "value": 2000, "sellable_value": 2000, "asset_class": "etf"}
    ],
    "open_orders": [
      {"order_id": "<broker-order-id>", "symbol": "QQQM", "side": "buy", "asset_class": "etf", "remaining_notional": 100}
    ],
    "coverage": {
      "positions_complete": true,
      "open_equity_orders_complete": true,
      "today_equity_orders_complete": true,
      "today_fills_complete": true,
      "advanced_orders_checked": true
    }
  },
  "reconciliation": {
    "local_intents": [{"order_id": "<broker-order-id>", "status": "queued"}],
    "broker_orders": [{"order_id": "<broker-order-id>", "status": "queued", "placed_agent": "agentic"}]
  },
  "review": null,
  "market_state": {"benchmark_a_pass": true, "benchmark_b_pass": true},
  "history": {"orders_today": 1, "turnover_today": 300, "used_decision_keys": []},
  "proposal": {"...": "see decision-card.md"}
}
```

注意：

- `cash_available_settled` 必须是现金账户的 `unleveraged_buying_power`，不是账面 cash。
- 持仓卖出上限用 `shares_available_for_sells * fresh quote`，不用总 quantity。
- `open_orders.remaining_notional` 必须含未成交剩余；闸门会把挂单纳入现金、仓位、单股和单标的限制。
- `advanced_orders_checked=true` 只能表示已通过独立可信渠道核对，不得将“工具不存在”填成 true。
- 完整账号不在此 JSON 中。

## 3. proposal 的订单表示

### 市价 DAY

```json
{
  "amount": 500,
  "quantity": null,
  "limit_price": null,
  "order_type": "market_day"
}
```

映射到 Robinhood：`type=market`, `dollar_amount="500.00"`, `market_hours=regular_hours`, `time_in_force=gfd`。

### 限价 DAY

```json
{
  "amount": 500,
  "quantity": 20,
  "limit_price": 25,
  "order_type": "limit_day"
}
```

映射到 Robinhood：`type=limit`, `quantity="20"`, `limit_price="25"`, `market_hours=regular_hours`, `time_in_force=gfd`。`amount` 必须与 quantity * limit_price 在 $0.01 内一致。Robinhood 限价单不支持只传美元金额。

## 4. post_review 包络

```json
{
  "phase": "post_review",
  "review": {
    "observed_at_et": "2026-08-13T15:41:30-04:00",
    "request": {
      "symbol": "ABCD",
      "side": "buy",
      "type": "market",
      "dollar_amount": "500.00",
      "market_hours": "regular_hours",
      "time_in_force": "gfd"
    },
    "response": {
      "symbol": "ABCD",
      "side": "buy",
      "type": "market",
      "dollar_amount": "500.00",
      "order_checks": {},
      "market_data_disclosure": "<Robinhood disclosure, preserved verbatim>",
      "quote_data": {
        "symbol": "ABCD",
        "state": "active",
        "has_traded": true,
        "bid_price": "25.00",
        "ask_price": "25.02"
      }
    }
  }
}
```

闸门校验 request/response/proposal 一致、时效、`order_checks == {}`、披露非空、标的 active/has_traded、买卖价有效以及价差不超 policy。完整 review 规范化哈希作为一次性指纹。

Robinhood 的实际回执没有 `preview_id`、`preflight_status`、`warnings` 或 `quoted_price`。任何使用这些旧字段的实现都与当前契约不兼容。

## 5. mandate v2

```json
{
  "mandate_version": "2.0",
  "mandate_id": "<uuid>",
  "account_ref_masked": "****0000",
  "execution_mode": "supervised_review",
  "requires_per_order_confirmation": true,
  "issued_at_et": "<aware ISO 8601>",
  "not_before_et": "<aware ISO 8601>",
  "expires_at_et": "<aware ISO 8601>",
  "max_order_amount": 600,
  "max_daily_turnover": 1500,
  "max_orders_per_day": 3,
  "policy_sha256": "<canonical policy hash>",
  "toolset_sha256": "<canonical toolset hash>",
  "confirmation": "I AUTHORIZE SUPERVISED ROBINHOOD TRADING 0000"
}
```

mandate 是 necessary but not sufficient：还必须有受信任账户绑定、当前工具面、无 HALT、完整账户覆盖、洁净 review 和当笔用户确认。Keychain 里的 mandate 哈希只用于检测文件漂移，不证明签发人身份。

## 6. 订单提交与幂等

- 每个逻辑订单首次提交生成一个 UUID `ref_id`。
- 传输超时时保留该 UUID；不得把未知当拒绝后换 UUID 重下。
- Robinhood `get_equity_orders` 不对外回显 `ref_id`。因此本地 ledger 必须在 place 成功回执时永久绑定 `ref_id -> order.id`；后续对账以 `order.id` 为准。
- 未得到 place 回执的超时只能标记 `submission_unknown`，停止新单并人工核对。

## 7. 状态集

活动：`new`, `queued`, `confirmed`, `unconfirmed`, `partially_filled`, `pending_cancelled`, `locating`。

终态：`filled`, `cancelled`, `rejected`, `failed`, `voided`, `partially_filled_rest_cancelled`, `locate_failed`。

未来出现任何未知状态时一律 fail-closed，先更新契约与回归测试。

## 8. 中止与 HALT

HALT 阻止一切新增风险的 review/place。对已有持仓的 `drawdown_action` / `thesis_break` 卖出提案，仍必须通过新鲜账户快照、review 和逐笔确认。
