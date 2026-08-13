# Codex Agentic Trader

一个面向 Robinhood 股票/ETF 的单一 Codex Skill：市场研究、现金账户快照、确定性风控、Robinhood 订单审核、逐笔确认、订单终态对账和复盘。

> 当前发布版是 `shadow` + `supervised_review`，不是无人值守自主实盘。Mandate 是有限委托，不是身份认证，也不能绕过 Robinhood 的审核与确认。

⚠️ 本项目为个人研究与自动化基础设施，不构成投资建议，也不能保证收益或限制最大损失。

## 为什么重新设计 mandate

旧设计把 mandate 说成“唯一实盘授权”和“Agent 无法自行签发”，这个安全结论并不成立：同一用户权限下的 Agent 可以创建 PTY、修改文件，甚至直接调用已暴露的券商工具。

v2 将 mandate 降回它真正擅长的角色：

- 给委托一个 `mandate_id`、起止时间和执行模式；
- 将单笔金额、每日换手和订单数限制在 policy 以下；
- 绑定 policy 与 Robinhood 工具清单的哈希，配置变化即失效；
- 为审核日志提供可追溯的委托上下文；
- 可单独撤销，使流程立即回到 shadow。

它是 necessary but not sufficient。真实执行仍需要 Robinhood 的 Agentic 账户权限、精确账户绑定、严格工具白名单、完整账户/订单覆盖、干净的 `review_equity_order` 回执，以及 review 后的逐笔明确确认。

## 能做什么

- VTI/QQQM 双基准趋势状态与 90/60/20% 股票暴露上限；
- 15/25/35% 现金流调整回撤分档和 HALT；
- 个股价格、市值、流动性、50/200 日线、3/6 月动量、20 日相对强度、财报窗口、新闻、普通股/可交易性与 wash sale 门禁；
- 单股 15%、个股合计 30%、最多 2 只、每日 6 笔、主动换手 30%；
- 将现有持仓和所有未完成买卖单一起纳入预测暴露、现金和可卖数量；
- 对齐当前 Robinhood 工具：`review_equity_order`、`place_equity_order`、`get_equity_orders`、`cancel_equity_order`；
- review 请求/响应绑定、行情披露、价差、新鲜度和重放检查；
- `ref_id -> order.id` 本地 ledger、完整订单状态机与 unknown fail-closed；
- 账户标识/凭据日志防泄漏、哈希链日志和行为偏差复盘。

## 三种能力层

| 层级 | 账户读取 | Robinhood 写操作 |
|---|---:|---:|
| `research` | 否 | 否 |
| `shadow` | 可选的固定单账户只读快照 | 否 |
| `supervised_review` | 固定单账户 | review 后向用户展示并逐笔确认，才可 place/cancel |

当前没有 `autonomous_live`。如果未来增加，必须使用模型不可绕过的固定账户执行 adapter，而不是仅靠 Skill 文本或 Python 脚本，并先完成独立安全审查和至少 10 个完整交易日 shadow。

## 安装

```bash
git clone https://github.com/Rroyyyyyyyyyyyy/agentic-trading-skills.git
cp -R agentic-trading-skills/skills/codex-agentic-trader ~/.codex/skills/
python3 ~/.codex/skills/codex-agentic-trader/scripts/test_desk.py
python3 ~/.codex/skills/codex-agentic-trader/scripts/test_codex_trader.py
```

只保留 `~/.codex/skills/codex-agentic-trader`；移除或归档旧的 Robinhood Skill，避免两个规则源同时生效。

## Robinhood 接入前提

1. 在 `policy/policy.json` 配置账户末四位、资金上限与策略参数。
2. 完整账户号只能由模型外的固定账户 adapter 注入，或由用户在当次任务中明确提供；不得用 `get_accounts` 枚举后猜默认账户。
3. 将 Robinhood MCP 的 `enabled_tools` 精确锁定为 `policy/enabled_tools_live.json`，然后运行：

```bash
codex mcp get robinhood-trading --json \
  | python3 ~/.codex/skills/codex-agentic-trader/scripts/runtime_scope_gate_live.py
```

4. shadow 运行至少 10 个完整交易日，验证快照分页、advanced order 覆盖、部分成交、取消竞态、买卖力、wash sale、日志与回滚。
5. 如确需监督式执行，由账户所有人在本地签发短期 mandate：

```bash
python3 ~/.codex/skills/codex-agentic-trader/scripts/mandate_admin.py create \
  --days 7 --max-order 300 --max-daily-turnover 750 --max-orders 3
```

6. 每笔仍先 `review_equity_order`，展示 Robinhood 返回的完整审核和原样 `market_data_disclosure`，取得明确确认后才可 `place_equity_order`。

## 当前已知边界

- Robinhood 账户读取工具要求完整 `account_number`，仅有末四位不能安全调用。
- 当前工具目录没有向本 Skill 保证 advanced/OCO 订单的完整读取；未被独立确认时，执行会 fail-closed。
- `get_equity_orders` 不回显 `ref_id`；提交回执必须立即持久化绑定 `ref_id -> order.id`。
- 本地哈希链可发现普通误改，但有文件写权限者可以重算整链。
- 双基准/均线体系是风险暴露开关，不是已经被证明能跑赢 VOO/QQQ 的 alpha 模型。
- 15% 单股上限不是 risk-per-trade 仓位模型；波动/失效距离 sizing 仍需后续 shadow 开发。

## 目录

```text
skills/codex-agentic-trader/
├── SKILL.md
├── agents/openai.yaml
├── policy/
├── references/
└── scripts/
    ├── decision_gate.py
    ├── live_gate.py
    ├── mandate_admin.py
    ├── order_ledger.py
    ├── runtime_scope_gate_live.py
    ├── journal_append.py
    ├── behavior_review.py
    └── test_*.py
```

## 参考

- [Robinhood Agentic Trading overview](https://robinhood.com/us/en/support/articles/agentic-trading-overview/)
- [Trading with your agent](https://robinhood.com/us/en/support/articles/trading-with-your-agent/)
- 订单状态、风险引擎和对账设计参考 Lean 与 NautilusTrader 的公开架构思想；没有复制其框架。

## License

MIT
