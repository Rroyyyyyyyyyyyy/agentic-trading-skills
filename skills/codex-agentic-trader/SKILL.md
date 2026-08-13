---
name: codex-agentic-trader
description: Codex 专用的 Robinhood 股票/ETF 研究、账户核对、决策风控、监督式订单执行和复盘 Skill。无 mandate 时只做 shadow；有效 mandate 也只是限时限额委托，仍必须使用 Robinhood 当前 review_equity_order 回执并获得逐笔明确确认。用于 Robinhood 市场研究、现金账户持仓/购买力/订单检查、盘前盘中收盘流程、下单前审核、订单终态对账和证据化策略迭代。
---

# Codex Agentic Trader

这是唯一的 Robinhood Skill。它把研究、确定性风控、Robinhood 审核、逐笔确认、订单对账和复盘放在同一契约中。不得与另一个 Robinhood Skill 并存或交叉执行。

策略研究见 [research-playbook.md](references/research-playbook.md)，决策卡见 [decision-card.md](references/decision-card.md)，执行契约见 [execution-contract.md](references/execution-contract.md)，不可越过的边界见 [hard-boundaries.md](references/hard-boundaries.md)。

## 先判断能做到哪一层

1. `research`：只读公开市场与新闻，不读账户。
2. `shadow`：可读受信任的单账户快照，跑全部决策闸门，但不调用 review/place/cancel。这是默认模式。
3. `supervised_review`：有效 mandate、无 HALT、工具面正确、账户精确绑定、快照与订单覆盖完整时，可以 review 订单。必须向用户展示完整审核与合规行情披露，得到该笔的明确确认后才可 place。

当前发布版没有 `autonomous_live`。普通“帮我下单”、历史授权、automation 或 mandate 都不能替代 Robinhood review 之后的逐笔确认。

## mandate 的意义

mandate 是可审计的有限委托包络，不是身份认证、账户绑定、平台授权或绕过确认的通行证。它只有五个价值：

- 明确谁在哪个时间窗口内委托了哪种执行模式；
- 将单笔额、每日换手和订单数压到 policy 之下；
- 绑定 policy 与工具清单指纹，任一漂移即失效；
- 在日志中给每次审核提供 mandate_id 与到期证据；
- 可独立撤销，使流程立即回到 shadow。

TTY 输入只是一个慎重的签发仪式，不能证明“Agent 无法自己签发”。同用户 shell 可以创建 PTY、改文件或调券商工具。真正的安全边界是 Robinhood 账户权限、MCP 工具白名单、精确账户绑定、平台审核/确认和独立操作人。

## 每次运行的固定序

### 0. 边界自检

```bash
codex mcp get robinhood-trading --json | python3 scripts/runtime_scope_gate_live.py
python3 scripts/live_gate.py --check-runtime
```

工具面不符、mandate 无效或 HALT 存在时，不得执行新增风险订单。`runtime_scope_gate_live.py` 只校验工具面，不证明账户绑定。

### 1. 确定时间与交易日

使用美东时间。研究可在盘前/收盘后做；审核和下单只能在策略允许的常规时段。脚本会拒绝周末和盘外时间；休市日仍以 Robinhood 交易日历/平台拒绝为最后边界。

### 2. 读取精确账户，绝不枚举

完整 `account_number` 只能来自宿主信任边界（例如固定账户 adapter/Keychain 注入）或当次用户明确给出的账户。不得调 `get_accounts` 来猜默认账户，不得读取其他账户。完整账号不得进入决策 JSON、日志、Git 或报告。

每轮刷新：

- `get_portfolio`：总净值、`buying_power.buying_power`、`unleveraged_buying_power`、pending deposits；
- `get_equity_positions`：持仓、成本、`shares_available_for_sells`；
- `get_equity_orders`：全部未完成订单、当日订单/成交，并处理全部分页；
- 本地 intent/order ledger：按 Robinhood `order.id` 对账；
- `get_equity_tradability`：待审核标的当前账户可交易性。

Robinhood 当前工具目录未向本 Skill 保证 advanced/OCO 订单的完整读取。若未由平台或独立操作人确认，`advanced_orders_checked` 必须为 false，执行 fail-closed。

### 3. 研究与提案

按 [research-playbook.md](references/research-playbook.md) 生成结构化提案。市场状态、回撤、持仓、挂单占用、已结算现金、wash sale、财报窗口和相对强度都必须是当轮数据。缺少反证或失效条件的提案直接放弃。

### 4. pre-review 确定性闸门

按 [execution-contract.md](references/execution-contract.md) 构造 JSON，运行：

```bash
python3 scripts/live_gate.py --input decision.json
```

只有 `mode=supervised` 且 `can_review=true` 才能调 `review_equity_order`。闸门会将挂单后的风险暴露与现金一并计算；不得把模型自报的百分比当成最终风控数字。

### 5. Robinhood review 与逐笔确认

用审批参数调 `review_equity_order`，然后刷新账户/订单/行情，将实际 request+response+observed_at_et 放入 `phase=post_review` 再跑闸门。

必须向用户展示：标的、方向、类型、金额/数量、限价、预计执行、`order_checks` 以及 `market_data_disclosure` 的原文。之后等待当笔明确确认。没有确认就不得调 `place_equity_order`。

### 6. 提交、终态与取消

确认后只能提交 `approved_intent` 对应的参数，常规时段 + GFD，并使用闸门产生的唯一 `ref_id`。首次调用超时时不得换 UUID 盲重试；先通过 `get_equity_orders` 查询状态。

`accepted`/`queued`/`confirmed` 不是成交。必须记录 `order.id`，跟踪 `partially_filled`、`filled`、`cancelled`、`rejected`、`failed`、`voided`、`partially_filled_rest_cancelled`、`locate_failed`等终态。任何未知状态都阻断新单。

取消也是真实写操作，必须再次明确确认。`cancel_equity_order accepted=true` 仅表示取消请求被接受，仍必须查到最终取消或与成交竞态的结果。

### 7. 日志与复盘

研究、决策、review、用户确认、submission、order_state、fill/cancel/HALT 分事件追加。追加失败即停止后续执行。日志链只能发现未重算的误改，不是防篡改系统。

复盘必须同时报告策略净值与 VOO/QQQ 同区间基准，费用/滑点口径一致；样本太短时不宣称跑赢。每天最多提出一个改进候选，不自动修改 active policy。

## 失败语义

- mandate 无效：回到 shadow，不是报错后绕过。
- HALT：阻止新增风险；只允许已有持仓的风险退出提案继续走 review+确认。
- 数据/分页/订单状态/可卖数量不完整：fail-closed。
- 账户绑定不可验证：只做无账户研究，不用 `get_accounts` 降级枚举。
- 平台警告、强制确认或契约漂移：展示并停止，不解释绕过。

## 安装后的最小验收

```bash
python3 scripts/test_desk.py
python3 scripts/test_codex_trader.py
python3 scripts/live_gate.py --check-runtime
```

在任何真实账户写操作前，先完成至少 10 个完整交易日的 shadow 评估，并由账户所有人审核工具面、账户绑定、mandate、日志、订单对账和回滚方案。
