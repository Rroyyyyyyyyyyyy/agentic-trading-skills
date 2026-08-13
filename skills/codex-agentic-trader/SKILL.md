---
name: codex-agentic-trader
description: Codex 专用的 Robinhood 股票/ETF 自动研究与交易 Skill。自动分析宏观、金融新闻、实时要事、市场趋势、持仓与候选股，在固定现金账户和硬风控内完成订单审核、提交、终态对账和复盘。用于盘前、盘中、收盘流程，以及 Robinhood 持仓/购买力/订单核对和合规股票/ETF 操作。
---

# Codex Agentic Trader

这是唯一的 Robinhood Skill，对用户只呈现一条流程：

```text
实时研究 → 后台账户/订单核对 → 确定性风控 → Robinhood 审核 → 合规提交 → 终态对账 → 复盘
```

用户不需要管理能力层、凭证或模式切换。账户、购买力、挂单和成交核对是每次操作前的内部必做步骤，不是用户额外流程。

策略研究见 [research-playbook.md](references/research-playbook.md)，决策卡见 [decision-card.md](references/decision-card.md)，订单契约见 [execution-contract.md](references/execution-contract.md)，硬边界见 [hard-boundaries.md](references/hard-boundaries.md)。

## 每次运行的固定顺序

### 1. 确认时间、交易日和工具面

使用美东时间。下单只允许在美股常规交易时段，只使用 DAY/GFD；休市、盘前、盘后只研究和对账。

```bash
codex mcp get robinhood-trading --json | python3 scripts/runtime_scope_gate_live.py
python3 scripts/live_gate.py --check-runtime
```

任一闸门失败就停止券商操作，但可继续无账户市场研究。

### 2. 后台精确锁定账户

每轮最多调用一次 `get_accounts`，只用于找到同时满足下列条件的唯一账户：

- 末四位等于 policy `account_last4`；
- active 现金账户；
- `agentic_allowed=true`。

匹配数不是 1 立即停止。完整账号只在当轮 Robinhood 工具参数中使用，不得进入 prompt、日志、Git、报告或消息。绝不读取、分析或操作其他账户。

### 3. 刷新真实状态

使用上一步的精确账户标识，处理全部分页并重新读取：

- `get_portfolio`：净值、真实 buying power、unleveraged buying power、pending deposits；
- `get_equity_positions`：持仓、成本和可卖数量；
- `get_equity_orders`：全部未完成订单、当日订单与成交；
- `get_option_positions` / `get_option_orders`：只用于确认禁止资产和占用，绝不操作期权；
- 本地 order ledger：按 Robinhood `order.id` 对账。

任何未知订单状态、分页缺失、买力不可验证、对账不平或同标的已有挂单都停止新单。

### 4. 实时研究与提案

按 [research-playbook.md](references/research-playbook.md) 重新搜索并分析：

- 美联储、利率、通胀、就业和当日宏观日历；
- 重大公司公告、财报、SEC/IR 一手来源；
- 地缘政治、行业轮动、市场广度、估值与波动；
- VTI、QQQM、SGOV、现有持仓和候选股的行情、趋势、新闻和财报窗口。

区分【事实】【市场反应】【推断】。没有最强反证和可观测失效条件的候选直接放弃。

### 5. 确定性 pre-review 闸门

按 [execution-contract.md](references/execution-contract.md) 构造当轮 JSON，运行：

```bash
python3 scripts/live_gate.py --input decision.json
```

闸门自行计算市场/回撤上限、持仓+挂单后的投影暴露、已结算现金、可卖额、日换手、订单数和防重键。只有 `can_review=true` 才进入下一步。

### 6. Robinhood 审核与提交

调用 `review_equity_order`，然后立即重新刷新购买力、持仓、未完成订单、当日成交和行情，把真实 request/response 绑定后再跑 `phase=post_review`。

仅在下列条件全部满足时调用 `place_equity_order`：

- `can_submit=true`；
- `order_checks == {}`；
- `market_data_disclosure` 存在；
- 审核参数与提案完全一致；
- 最新买力、持仓、挂单、成交和风险仍然通过；
- 当前仍在常规交易时段。

每个逻辑订单使用一个独立 UUID `ref_id`。用户已在 policy 边界内预先授权合规订单，不要重复询问普通逐笔确认。但 Robinhood 如果强制平台确认、要求披露确认或返回实质性警告，必须停止并报告，不得绕过。

### 7. 终态对账

`accepted` / `queued` / `confirmed` 不是成交。立即记录 Robinhood `order.id`，持续查询到已知终态。首次调用超时时保留原 UUID，先查订单再决定是否重试，不得换 UUID 盲目重下。

取消也必须核对最新成交和剩余数量。`cancel_equity_order accepted=true` 只表示取消请求已接收，必须再查到最终取消或与成交竞态的结果。

### 8. 日志与复盘

研究、决策、review、submission、order_state、fill/cancel/HALT 分事件追加。追加失败即停止后续操作。链式哈希只防误改，不宣称密码学防篡改。

复盘同时报告策略净值与 VOO/QQQ 同区间基准，口径一致；样本少于 20 个交易日或存在基线重置时不宣称跑赢。

## 失败语义

- 账户匹配不唯一、帐户类型不可验证或完整账号不可用：不做账户读取或交易。
- 数据、分页、订单状态、可卖数量、买力或对账不完整：不下单。
- 平台警告、强制确认、契约漂移、未知提交结果：停止并报告。
- HALT 存在时阻止新增风险；只允许已有持仓的风险退出继续过闸门。

## 最小验收

```bash
python3 scripts/test_desk.py
python3 scripts/test_codex_trader.py
python3 scripts/live_gate.py --check-runtime
```
