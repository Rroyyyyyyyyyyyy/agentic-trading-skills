# 执行契约（live_gate.py 的输入输出与两阶段流程）

## 1. 两阶段流程总览

```
决策 JSON（phase=pre_preview）
  → live_gate：mandate/HALT/对账/市场状态/回撤/仓位/频次/准入 全检
  → can_preview=true 才调券商 preview（shadow 模式到此为止，记 shadow_would_allow）
  → preview 回执并入 JSON（phase=post_preview，账户与行情必须刷新）
  → live_gate 复检 + preview 校验
  → can_submit=true → 提交与 intent 完全一致的订单（独立 client key）
  → 按订单 ID 查询至终态；accepted/queued ≠ fill
```

## 2. 输入 JSON（在 roy-trading-desk 决策契约基础上扩展）

基础字段（schema_version/as_of_et/data_age_seconds/account/market_state/history/proposal 及 declares）见本目录 [decision-card.md](decision-card.md)（基础契约），扩展以下字段：

```json
{
  "phase": "pre_preview",
  "reconciliation": {
    "local_intents": [
      {"client_key": "<uuid>", "status": "filled"}
    ],
    "broker_orders": [
      {"client_key": "<uuid>", "status": "filled"}
    ]
  },
  "preview": null
}
```

- `mode` 字段不存在——模式由闸门根据 mandate 状态推导，会话无权声明。
- `reconciliation`：本地 intent 台账与券商**当日+未完成**订单全集。规则：
  - 每个本地非终态 intent 必须能在 broker_orders 找到同 client_key；
  - 每个 broker_order 的 client_key 必须存在于本地台账；
  - `status` 只认 `open|partially_filled|filled|cancelled|rejected|unknown`；
  - 任何不匹配或 `unknown` → `reconciliation_mismatch`，拒绝新单（撤单/查询不受限）。
- `phase=post_preview` 时 `preview` 必填，且**必须携带提案回显字段**（2026-08-13 对抗审查 P1 修复）：

```json
{
  "preview": {
    "preview_id": "<券商返回>",
    "preflight_status": "clean",
    "warnings": [],
    "quoted_price": 25.05,
    "preview_age_seconds": 30,
    "symbol": "ABCD",
    "side": "buy",
    "amount": 500.0
  }
}
```

  校验：`preflight_status` 恰为 `clean`、`warnings` 为空数组、`preview_age_seconds ≤ policy.max_preview_age_seconds`、`quoted_price` 为正有限数；**`symbol`/`side`/`amount` 必须与 proposal 逐字段一致**（金额容差 $0.01）——陈旧或无关回执过不了闸门；**`preview_id` 一次性消费**——出现在闸门状态账本或 `history.used_preview_ids` 中即拒（`preview_replayed`）。任何不满足 = 拒绝，无二次机会（重新走 pre_preview）。
- `order_type=limit_day` 时 proposal 必须带 `limit_price`（正有限数）；`market_day` 不得带（P4 修复）。
- mandate 到期日按严格 ISO 日历解析（`date.fromisoformat`），任何非规范串（`2026-13-45`、空格补位等）一律视为过期（P3 修复）。

## 2b. 闸门状态账本（P2 修复）

闸门在 `policy.gate_state_file` 维护当日已批订单的本地账本（decision_key / preview_id / amount）。评估时**频次、换手、幂等、preview 重放取「调用方声明 ∪ 账本」的更严值**——调用方谎报 `orders_today=0` 也压不掉账本里的真实计数。CLI 路径每次 `can_submit=true` 自动入账；账本损坏 = `gate_state_corrupt` 拒绝。换 ET 日期自动清零。账本仍是同用户文件（诚实边界同 §6），但它把"逐次调用各自自证"收敛为"闸门单点记账"。

## 2c. HALT 语义（P5 修复）

- HALT 文件存在：只放行**风险退出卖单**（`side=sell` 且 trigger ∈ risk_exit_triggers，即 hard-boundaries §4 的清仓通道），其余提案一律 `halt_active` 拒绝。
- 回撤触及最深档：闸门输出 `halt_required=true` 且 **CLI 路径自动创建 HALT 文件（自锁）**，不再依赖 runner 履约；HALT 解除仍只能 Roy 手动删文件。

## 3. 输出

```json
{
  "mode": "live",
  "phase": "pre_preview",
  "would_allow": true,
  "can_preview": true,
  "can_submit": false,
  "halt_required": false,
  "decision_key": "<sha256>",
  "violations": [],
  "derived": {"...": "同 roy-trading-desk，另含 mandate_remaining_days 等"},
  "approved_intent": {"symbol": "...", "side": "...", "amount": 0.0, "asset_class": "...", "order_type": "market_day", "limit_price": null, "trigger": "...", "decision_key": "<sha256>", "preview_id": "<回执ID>", "quoted_price": 0.0, "client_key_required": true}
}
```

- `shadow` 模式：`can_preview`/`can_submit` 恒 false，放行判定记在 `shadow_would_allow`。
- `live + pre_preview`：最多给 `can_preview=true`。
- `live + post_preview`：全部通过才 `can_submit=true`，同时回显 `approved_intent`；提交内容必须与之逐字段一致。
- `halt_required=true`：回撤触及最深档。运行方职责（闸门管不到执行侧，必须由 runner 完成）：
  1. 立即 `touch <policy.halt_file>`；
  2. 仅允许提交风险退出卖单（trigger=drawdown_action，仍走两阶段）；
  3. 日报置顶报告。

## 4. mandate 文件契约（mandate_admin.py 管理）

```json
{
  "mandate_version": "1.0",
  "account_last4": "0000",
  "issued_at_et": "2026-08-13",
  "expires_at_et": "2026-09-12",
  "max_order_amount": 600.0,
  "max_daily_turnover": 1500.0,
  "confirmation": "I AUTHORIZE LIVE TRADING 0000"
}
```

- 生效条件：文件存在且 0600 权限、未过期、`account_last4` 与 policy 一致、confirmation 语句完全匹配、（macOS）Keychain 中的 sha256 与文件当前内容一致。
- 校验和存 Keychain 的目的是**漂移检测**（文件被改动即失效），不是密码学防篡改——同用户 shell 可以两处一起改，诚实边界见 hard-boundaries §6。
- mandate 上限与 policy 上限取更严者（例：mandate `max_order_amount=600` 会把单笔压到 ≤$600，即使 policy 允许更大）。

## 5. decision_key 与幂等

`decision_key = sha256(ET日期|symbol|side|trigger|amount.2f)`，与 used_decision_keys 查重；此外每笔提交必须使用独立 `client_key`（UUID），重试必须复用原 client_key。decision_key 防手滑，client_key + 券商对账防重复提交——两层都在才算幂等。
