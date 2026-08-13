# Agentic Trading Skills · AI 交易研究台

> **决策交给 AI，风控交给代码，执行分级授权。**
> 一对可以直接装进 AI 编程助手（Claude Code / OpenAI Codex）的个人交易 Agent Skill：
> 一个"AI 出脑、你出手"的手动执行版，一个"限期授权内全自动"的实盘版。

⚠️ **本项目为个人研究工具，不构成任何投资建议。市场有风险，实盘盈亏自负。**

---

## 这是什么？能做什么？

给 AI 助手装上这套 skill 后，它就变成你的**个人交易台**，能做这些事：

| 能力 | 说明 |
|------|------|
| 📊 **每日市场研判** | 双基准 ETF 趋势信号（6 个月回报 + 200 日均线，收盘确认）推导当日股票仓位上限档；宏观与新闻检索（美联储/BLS/SEC 一手来源优先，附链接，区分事实/市场反应/推断） |
| 🔍 **候选股研究** | 量化漏斗初筛（价格/市值/流动性/双均线/动量/相对强度/财报回避），每个候选强制产出"论点 / 反论点 / 失效条件"三件套，缺一即弃 |
| 🎯 **决策卡生成** | 每笔操作先过**确定性代码闸门**（不是让 LLM 自己拍脑袋），放行才出卡：标的/方向/金额/订单类型/触发器/失效条件一应俱全 |
| 🛡️ **硬风控** | 市场状态仓位上限（90/60/20%）、**全局回撤门**（回撤 15%→股票上限 70%、25%→40%、35%→清仓+断路停机）、单股 ≤15%/个股合计 ≤30%、每日 ≤6 笔、日换手 ≤30%、禁杠杆/期权/加密/仙股/OTC——全部代码强制，LLM 无法覆盖 |
| 📒 **链式交易日志** | SHA-256 链式 JSONL 记录每张决策卡、每笔成交、每次偏离；自动拦截凭据与疑似账号泄漏；追加失败即停 |
| 🪞 **行为偏差画像** | 复盘你自己的交易数据，检测四类行为偏差：过度交易、处置效应（卖盈太快留亏太久）、追涨买入、决策卡执行偏离——并给出 if-then 对策建议 |
| 🤖 **全自动实盘**（仅 Codex 版） | 在你签发的限期限额 mandate 内自主执行：两阶段下单（闸门→券商 preview→复检→提交→按订单 ID 验证至终态）、对账幂等、HALT 一键停机 |

### 两个 skill 怎么选？

| | `roy-trading-desk`（手动版） | `codex-agentic-trader`（实盘版） |
|---|---|---|
| 宿主 | Claude Code（或任何支持 Anthropic 风格 skill 的 agent） | OpenAI Codex |
| 执行方式 | AI 出决策卡 → **你在券商 App 手动下单** → 回报 AI 记账 | mandate 有效期内 **AI 自动下单**（两阶段闸门保驾） |
| 授权模型 | 无需授权——AI 结构上就没有下单能力 | 你亲自在终端签发 mandate（限期+限额，agent 够不着签发入口） |
| 适合谁 | 想保留最终扣扳机权的人；使用 Claude 的人 | 想要"完全不用管"的人（Claude 有厂商级禁令不能执行交易，所以实盘版只能跑在 Codex 上） |
| 兜底 | 决策卡过期作废、闸门 fail-closed | 同左 + HALT 哨兵文件 + mandate 随时撤销 + 回撤断路器 |

两个 skill 共享同一套决策契约与基础闸门代码（`decision_gate.py`），实盘版在其上叠加 mandate / 对账 / 两阶段 / HALT。

---

## 设计哲学（为什么不是"又一个让 GPT 帮你炒股的 prompt"）

1. **闸门在 LLM 之外**。能不能买、买多少，由确定性 Python 脚本推导（仓位、回撤、频次全部内部计算，不信任 LLM 的声明）；LLM 只负责研究和写论点。任何一条检查失败 = 拒绝，AI 不得"解释绕过"。
2. **授权动作 agent 不可达**（借鉴 [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) 的 mandate 思想）。实盘授权靠你在交互终端输入完整确认语签发，非交互调用直接失败——AI 无法给自己授权。
3. **fail-closed 文化**。数据缺失不猜、行情过期不用、对账不平不下新单、审计写不进就停机。宁可错过，不可错下。
4. **补上开源同类普遍缺失的全局回撤门**。大多数交易 agent 只有单笔止损；本项目按净值高水位分档降仓，最深档自动写 HALT 停机。
5. **诚实边界写在明面上**。日志链防误改不防篡改；同用户 shell 不是密码学隔离；闸门验逻辑不验数据真伪。这些限制都写在 `hard-boundaries.md` 里，不假装自己是银行级安全。

## 架构

```
┌─ LLM（研究/论点/候选排序 —— 无权计算最终敞口）
│
│   决策 JSON
│   ▼
├─ decision_gate.py / live_gate.py   ← 确定性闸门（唯一放行通道）
│   ├─ 市场状态仓位上限（双基准 ETF 趋势）
│   ├─ 全局回撤门（15/25/35% 三档 + 断路器）
│   ├─ 个股准入（价格/市值/流动性/趋势/财报回避）
│   ├─ 仓位/频次/换手/幂等/新鲜度
│   └─ [实盘版] mandate + HALT + 对账 + 两阶段 preview
│   ▼
├─ 手动版：决策卡 → 人在券商 App 执行 → 回报记账
├─ 实盘版：preview → 复检 → 提交 → 按订单 ID 验证至终态
│   ▼
└─ journal_append.py（SHA-256 链式日志）→ behavior_review.py（行为画像）
```

---

## 安装

### 手动版（Claude Code）

```bash
git clone https://github.com/Rroyyyyyyyyyyyy/agentic-trading-skills.git
cp -R agentic-trading-skills/skills/roy-trading-desk ~/.claude/skills/
python3 ~/.claude/skills/roy-trading-desk/scripts/test_desk.py   # 应 48 项全绿
```

然后在 Claude Code 里说一句：

> 用 roy-trading-desk 做今天的市场研判

### 实盘版（OpenAI Codex）

```bash
cp -R agentic-trading-skills/skills/codex-agentic-trader ~/.codex/skills/
python3 ~/.codex/skills/codex-agentic-trader/scripts/test_codex_trader.py   # 应 29 项全绿
```

激活步骤（缺一不可，详见 skill 内 `SKILL.md`）：

1. 把 `policy/policy.json` 的 `account_last4` 改成你自己账户的末四位，按需调整资金与仓位参数；
2. 在 Codex 完成券商 MCP OAuth（本项目按 Robinhood Agentic Trading MCP 设计），用 `enabled_tools` 白名单锁死工具面，跑 `runtime_scope_gate_live.py` 验证；
3. **shadow 模式试运行**（默认态，建议 ≥10 个交易日）：全流程走到 preview 之前为止，零真实下单，验证对账/闸门/日报；
4. 你本人在终端签发 mandate（这是唯一的实盘开关）：

```bash
python3 ~/.codex/skills/codex-agentic-trader/scripts/mandate_admin.py create --days 30 --max-order 600 --max-daily-turnover 1500
```

5. 按 `references/automation-prompt.md` 模板在 Codex 建每日 automation（美东 8:00–17:30，盘中每 30 分钟决策循环 + 收盘复盘日报）。

**紧急停止（任一立即生效）：**

```bash
touch ~/.codex-agentic-trader/HALT        # 哨兵停机
```

```bash
python3 ~/.codex/skills/codex-agentic-trader/scripts/mandate_admin.py revoke   # 撤销授权
```

---

## 快速体验（不连券商也能玩）

闸门是纯本地脚本，构造一个决策 JSON 就能看到它怎么工作：

```bash
python3 skills/roy-trading-desk/scripts/decision_gate.py --input examples/demo-decision.json
```

输出（放行时）：

```json
{"would_allow": true, "decision_key": "…", "violations": [],
 "derived": {"drawdown": 0.02, "stock_cap_effective": 0.9, "equity_exposure_after": 0.54},
 "manual_card": {"action": "BUY QQQM $500.00", "…": "…"},
 "execution_capability": "none"}
```

把回撤调到 30%、把仓位怼超、挑一只 $3 的仙股——看它逐条拒绝，就理解这套东西的脾气了。

## 目录结构

```
skills/
├── roy-trading-desk/            # 手动执行版（Claude）
│   ├── SKILL.md                 # 五模式工作流：research / decide / record / review / evolve
│   ├── references/              # 硬边界 / 决策契约与决策卡格式 / 研究手册
│   ├── scripts/                 # decision_gate / journal_append / behavior_review / 48 项测试
│   └── policy/policy.json       # 全部策略参数（只有账户所有人能改）
└── codex-agentic-trader/        # 实盘版（Codex）
    ├── SKILL.md                 # 固定运行序 + 模式推导 + 激活步骤
    ├── references/              # 实盘硬边界 / 执行契约（两阶段）/ automation 模板
    ├── scripts/                 # live_gate / mandate_admin / scope gate / 29 项测试
    └── policy/                  # policy.json + MCP 工具白名单
```

## 常见问题

**Q：为什么手动版跑在 Claude、实盘版跑在 Codex？**
A：Anthropic 给 Claude 内置了不可解除的禁令——任何情况下不执行金融交易（用户授权也不行）。所以 Claude 版的定位就是"最强研究员+风控官"，扣扳机的永远是你；想要全自动，执行者只能是没有这条禁令的 agent（本项目适配了 Codex）。

**Q：策略参数是通用的吗？**
A：默认参数按一个约 $5,000 的小型实验账户设定（双 ETF 核心 + 最多 2 只个股卫星仓）。全部参数在 `policy.json`，按你的账户规模和风险偏好改——但**先跑测试再上线**。

**Q：数据从哪来？**
A：skill 本身不绑数据源。行情优先用宿主 agent 可用的市场 MCP（如 Robinhood 官方 Agentic MCP 的只读行情工具），没有就用 Web 检索一手来源；闸门只认新鲜数据（默认 ≤300 秒）。

**Q：真的安全吗？**
A：诚实回答：比"纯 prompt 让 LLM 自律"安全一个量级（风控是代码强制的），但同用户 shell 不是密码学隔离、日志链不防全文件重写、闸门验逻辑不验数据真伪。这也是默认参数把规模压在实验档的原因。别拿它跑你亏不起的钱。

## 致谢

- [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading)：mandate 授权制、HALT 哨兵、行为画像（Shadow Account）思想
- [tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills)：只分析不下单的 skill 定位
- 结构化交易系统的对账与状态机模式参考了 nautilus_trader 与 Lean 的公开设计

## 免责声明

本项目全部输出为规则化研究产物与个人决策辅助工具，**不构成投资建议**；作者与贡献者不对任何使用本项目产生的盈亏负责。使用实盘版即代表你理解两阶段闸门与 mandate 机制的能力边界，并自愿承担全部执行风险。

## License

MIT
