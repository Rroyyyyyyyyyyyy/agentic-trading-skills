# Codex Agentic Trader

一个面向 Robinhood 美国上市普通股和普通 ETF 的单一 Codex Skill。它将实时市场研究、金融新闻、账户/订单后台核对、确定性风控、Robinhood 订单审核、合规提交、终态对账和复盘放在一条固定流程中。

> 用户不需要管理多套能力层或额外凭证。但每次券商操作前的账户、购买力、挂单、成交和审核回执核对仍是内部必做步骤。

⚠️ 本项目为个人研究与自动化基础设施，不构成投资建议，不保证收益或限制最大损失。

## 能做什么

- 分析美联储、利率、通胀、就业、当日宏观日历和重大实时事件；
- 检索 SEC/公司 IR、财报、行业轮动、市场广度、估值与波动证据；
- 计算 VTI/QQQM 双基准趋势与 90/60/20% 股票暴露上限；
- 执行 15/25/35% 净现金流调整回撤分档和 HALT；
- 用价格、市值、流动性、50/200 日线、3/6 月动量、20 日相对强度、财报窗口、新闻、普通股身份和 wash sale 筛选候选；
- 将现有持仓和全部未完成买卖单纳入投影暴露、现金和可卖数量；
- 在通过两阶段闸门后调用 Robinhood `review_equity_order` 与 `place_equity_order`；
- 用唯一 `ref_id`、Robinhood `order.id` 和本地 ledger 追踪部分成交、取消竞态和终态；
- 用净现金流调整绩效与 VOO/QQQ 基准做证据化复盘。

## 单一流程

```text
实时研究
  → 唯一账户匹配
  → portfolio / positions / orders / fills 全量刷新
  → 策略与风控闸门
  → Robinhood order review
  → 再次刷新全部可变状态
  → review 回执绑定闸门
  → 合规提交
  → 订单终态对账
```

用户不会被要求管理这些内部步骤。它们的作用是防止重复下单、使用未结算现金、超过可卖数量或把已接收误报成已成交。

## 安装

```bash
git clone https://github.com/Rroyyyyyyyyyyyy/agentic-trading-skills.git
cp -R agentic-trading-skills/skills/codex-agentic-trader ~/.codex/skills/
python3 ~/.codex/skills/codex-agentic-trader/scripts/test_desk.py
python3 ~/.codex/skills/codex-agentic-trader/scripts/test_codex_trader.py
```

只保留 `~/.codex/skills/codex-agentic-trader`，避免两个 Robinhood 规则源同时生效。

## Robinhood 接入

1. 连接官方 Robinhood MCP：

```bash
codex mcp add robinhood-trading --url https://agent.robinhood.com/mcp/trading
```

2. 在 `policy/policy.json` 设置获得明确授权的现金账户末四位、本金上限和风控参数。
3. 将 Robinhood MCP `enabled_tools` 精确限定为 `policy/enabled_tools_live.json` 中的工具，不得开放转账、提现、账户设置、期权交易或加密货币工具。
4. 验证：

```bash
codex mcp get robinhood-trading --json \
  | python3 ~/.codex/skills/codex-agentic-trader/scripts/runtime_scope_gate_live.py
python3 ~/.codex/skills/codex-agentic-trader/scripts/live_gate.py --check-runtime
```

5. 完全退出并重新打开 Codex，使新工具清单进入新任务。

## 不可放宽的边界

- 每轮 `get_accounts` 最多一次，必须唯一匹配设定末四位、active cash、`agentic_allowed=true`；
- 禁止读取、分析或操作其他账户；
- 完整账号不得进入 prompt、日志、Git 或报告；
- 禁止做空、保证金、杠杆/反向 ETF、期权、加密货币、OTC、低价股、转账、提现和账户设置；
- 仅常规交易时段 + GFD；
- Robinhood 强制确认、披露确认或实质性警告一律停止，不得绕过；
- `accepted` / `queued` 不是成交，必须查到订单最终状态。

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
