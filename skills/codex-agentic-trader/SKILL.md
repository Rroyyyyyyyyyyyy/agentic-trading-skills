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

每天只做两次完整分析：08:30 ET 盘前与 17:30 ET 收盘后。17:30 分析结束后只生成一个当日版本的日报，并完成当日知识留存与进化评估。

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

### 4. 按购买力决定盘中判断频率

把最新 `unleveraged_buying_power`、交易日状态和风险异常传入：

```bash
python3 scripts/cadence_gate.py --input cadence.json
```

- 购买力至少覆盖最小订单金额和现金缓冲时，09:30-16:00 ET 每 30 分钟做一次操作判断；
- 购买力不足时，每 30 分钟只快速核对购买力、挂单和成交，仅在 10:00、12:00、14:00、16:00 ET 做完整买卖判断；
- 持仓逻辑破坏、回撤风控、重大突发风险或订单异常不等待两小时；
- cadence 只决定是否进入判断，不授权交易。账户或购买力不可验证仍 fail closed。

### 5. 实时研究与提案

按 [research-playbook.md](references/research-playbook.md) 重新搜索并分析：

- 美联储、利率、通胀、就业和当日宏观日历；
- 重大公司公告、财报、SEC/IR 一手来源；
- 地缘政治、行业轮动、市场广度、估值与波动；
- VTI、QQQM、SGOV、现有持仓和候选股的行情、趋势、新闻和财报窗口。

区分【事实】【市场反应】【推断】。没有最强反证和可观测失效条件的候选直接放弃。

一级绩效目标是在完整 5,000 美元策略本金可用后的下一交易日起，连续 100 个日历日实现净利润 2,500 美元。每日目标是在每个完成的常规交易日，用净现金流调整且扣费用/滑点的策略当日收益同时高于 VOO 与 QQQ 当日收益；这是每日评分目标，不是可保证结果。

运行 `performance_review.py` 同时计算挑战、每日双基准评分、滚动基准差和风险姿态。目标利润按锁定的 Day 1 基线净值计算，目标净值是“基线 + 2,500 美元”，不得把挑战前盈亏或后续外部现金流算进目标进度。

只有以下条件同时满足才进入 `controlled_offense`：挑战至少第10个日历日；相对线性目标落后至少250美元且完成度低于65%；滚动至少5个交易日落后任一基准；VTI与QQQM基础趋势均通过；账户回撤低于10%；波动为低/正常；账户/订单快照完整；无重大风险、无HALT。受控进攻把参考配置从 ETF/防御资产倾向合格个股：QQQM 35%、VTI 25%、个股合计最多30%、SGOV/现金至少10%；单股仍最多15%、最多2只。任何条件失败就保持正常姿态。

受控进攻仍不直接授权订单。只有候选通过全部价格、市值、流动性、趋势、动量、相对强度、财报、新闻、wash sale、仓位、换手和 Robinhood review 门禁时才可建仓或轮换。挑战进度、单日输给基准或滚动落后都不能单独触发交易，不能降低准入门槛、使用杠杆或保证利润。

### 6. 确定性 pre-review 闸门

按 [execution-contract.md](references/execution-contract.md) 构造当轮 JSON，运行：

```bash
python3 scripts/live_gate.py --input decision.json
```

闸门自行计算市场/回撤上限、持仓+挂单后的投影暴露、已结算现金、可卖额、日换手、订单数和防重键。只有 `can_review=true` 才进入下一步。

### 7. Robinhood 审核与提交

调用 `review_equity_order`，然后立即重新刷新购买力、持仓、未完成订单、当日成交和行情，把真实 request/response 绑定后再跑 `phase=post_review`。

仅在下列条件全部满足时调用 `place_equity_order`：

- `can_submit=true`；
- `order_checks == {}`；
- `market_data_disclosure` 存在；
- 审核参数与提案完全一致；
- 最新买力、持仓、挂单、成交和风险仍然通过；
- 当前仍在常规交易时段。

每个逻辑订单使用一个独立 UUID `ref_id`。用户已在 policy 边界内预先授权合规订单，不要重复询问普通逐笔确认。但 Robinhood 如果强制平台确认、要求披露确认或返回实质性警告，必须停止并报告，不得绕过。

### 8. 终态对账

`accepted` / `queued` / `confirmed` 不是成交。立即记录 Robinhood `order.id`，持续查询到已知终态。首次调用超时时保留原 UUID，先查订单再决定是否重试，不得换 UUID 盲目重下。

取消也必须核对最新成交和剩余数量。`cancel_equity_order accepted=true` 只表示取消请求已接收，必须再查到最终取消或与成交竞态的结果。

### 9. 日志、日报与进化

研究、决策、review、submission、order_state、fill/cancel/HALT 分事件追加。追加失败即停止后续操作。链式哈希只防误改，不宣称密码学防篡改。

17:30 ET 完成唯一日报：当日真实操作、盘前/盘后结论、持仓/现金/净值、风险、当日损益、最大回撤、100天挑战 Day N/100、挑战净利润与剩余差额、策略/VOO/QQQ当日收益和当日胜负、滚动超额差、正常或受控进攻姿态及原因、下一交易日触发条件。相同正文发送给多个既有收件人仍算一个日报版本；不得盘中重复发送日报。

收盘后追加一个 `daily_review` 事件，保存事实、决策、成交结果、错失机会、反证、当日双基准胜负、滚动基准差、挑战进度、风险姿态、最强教训和次日条件。下一次盘前分析先读取最近 20 个 `daily_review` 与成交行为统计，让新分析继承历史证据。每天最多提出一个有测试、评价指标和回滚方案的改进；自动积累知识不等于自动放宽 policy、账户边界或风险上限。

复盘的策略收益与挑战净利润必须净现金流调整并扣费用/滑点，与 VOO/QQQ 使用相同区间；样本少于 20 个交易日或存在基线重置时不宣称跑赢。挑战基线不可为了改善进度重置；缺少可验证基线时写“无法获取”，不得用 5,000 美元或其他估计值代替。

## 失败语义

- 账户匹配不唯一、帐户类型不可验证或完整账号不可用：不做账户读取或交易。
- 数据、分页、订单状态、可卖数量、买力或对账不完整：不下单。
- 平台警告、强制确认、契约漂移、未知提交结果：停止并报告。
- HALT 存在时阻止新增风险；只允许已有持仓的风险退出继续过闸门。

## 最小验收

```bash
python3 scripts/test_desk.py
python3 scripts/test_codex_trader.py
python3 scripts/cadence_gate.py --input cadence.json
python3 scripts/performance_review.py --input performance.json
python3 scripts/live_gate.py --check-runtime
```
