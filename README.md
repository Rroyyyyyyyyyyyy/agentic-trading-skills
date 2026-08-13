# Codex Agentic Trader · AI 自主交易 Skill

> **决策交给 AI，风控交给代码，授权交给 mandate。**
> 一个装进 OpenAI Codex 的个人交易 Agent Skill：在你亲自签发的限期限额授权内，
> AI 自主研判并执行美股/ETF 订单；授权之外，它只是一个出决策卡的研究员。

⚠️ **本项目为个人研究工具，不构成任何投资建议。市场有风险，实盘盈亏自负。**

---

## 这是干什么的？

给 Codex 装上这个 skill，它就变成一个**带硬风控的全自动个人交易台**：

| 能力 | 说明 |
|------|------|
| 📊 **每日市场研判** | 双基准 ETF 趋势信号（6 个月回报 + 200 日均线，收盘确认）推导当日股票仓位上限档；宏观与新闻检索（美联储/BLS/SEC 一手来源优先，附链接，区分事实/市场反应/推断） |
| 🔍 **候选股研究** | 量化漏斗（价格/市值/流动性/双均线/动量/相对强度/财报回避），每个候选强制"论点 / 反论点 / 失效条件"三件套，缺一即弃 |
| 🤖 **mandate 内自主执行** | 你在终端签一张限期限额授权（默认 30 天），有效期内 AI 走两阶段闸门自动下单：确定性检查 → 券商 preview → 回执绑定复检 → 提交 → 按订单 ID 验证至终态 |
| 🛡️ **代码级硬风控** | 市场状态仓位上限（90/60/20%）、**全局回撤门**（15%→股票上限70%、25%→40%、35%→清仓+自动 HALT 停机）、单股 ≤15%/个股合计 ≤30%、每日 ≤6 笔、日换手 ≤30%、禁杠杆/期权/加密/仙股/OTC——全部 Python 脚本强制，LLM 无法覆盖 |
| 🧾 **闸门状态账本** | 闸门自己记当日已批订单/金额/decision_key/preview 回执——频次、换手、幂等不信任调用方声明，谎报压不掉账本 |
| 📒 **链式交易日志** | SHA-256 链式 JSONL 记录决策/preview/提交/终态；自动拦截凭据与疑似账号泄漏；写不进就停机 |
| 🪞 **行为偏差画像** | 复盘成交数据，检测过度交易、处置效应、追涨、决策偏离，给 if-then 对策 |
| 🛑 **三重急停** | `touch HALT` 哨兵（停机中仍允许清仓降风险）、`mandate revoke` 撤权、回撤断路器自锁——任一立即生效 |

**没有 mandate 时**（默认态）它自动运行在 **shadow 模式**：全流程研判照跑、决策卡照出，但零券商写操作——出的卡由你手动执行。想全自动再签授权，想收权随时撤。

## 设计哲学（为什么不是"又一个让 GPT 炒股的 prompt"）

1. **闸门在 LLM 之外**。能不能买、买多少由确定性 Python 脚本推导（仓位/回撤/频次全部内部计算）；LLM 只负责研究和论点，任何一条检查失败 = 拒绝，AI 不得"解释绕过"。
2. **授权动作 agent 不可达**（借鉴 [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading) 的 mandate 思想）。签发 mandate 需要在交互终端输入完整确认语，非交互调用直接失败——AI 无法给自己授权，也被明令禁止诱导你续签。
3. **两阶段执行 + 回执绑定**。券商 preview 回执必须与提案逐字段一致（标的/方向/金额）且一次性消费——陈旧、无关、重放的回执全部过不了闸门；限价单必须带限价，批准的 intent 与 preview_id/decision_key/报价绑定锁死。
4. **fail-closed 文化**。数据缺失不猜、行情过期不用、对账不平不下新单、账本写不进就撤销放行。宁可错过，不可错下。
5. **补上开源同类普遍缺失的全局回撤门**。按净值高水位分档降仓，最深档闸门**自动写 HALT 自锁**，解锁只能人来。
6. **诚实边界写在明面上**。日志链防误改不防篡改、同用户 shell 不是密码学隔离、闸门验逻辑不验数据真伪——都写在 `hard-boundaries.md`，不假装银行级安全，所以默认参数把规模压在实验档（$5,000）。
7. **经过两轮对抗审查**。审查员实测攻击闸门（92 项回归测试沉淀），修复了 preview 重放、幽灵仓位绕过持仓上限、畸形日期绕过 mandate 到期、账号扫描器绕过等十余项——审查记录就是测试用例。

## 架构

```
┌─ Codex（研究/论点/候选排序 —— 无权计算最终敞口，无权自我授权）
│   ▼ 决策 JSON
├─ live_gate.py ─ 确定性闸门（唯一放行通道）
│   ├─ mandate 校验（存在/未过期/额度/校验和）→ 无效自动降级 shadow
│   ├─ HALT 哨兵（存在时只放行清仓类风险退出卖单）
│   ├─ 基础层 decision_gate.py：市场状态/回撤/准入/仓位/频次/幂等/新鲜度
│   ├─ 本地/券商订单对账（不平拒新单）
│   ├─ 状态账本（当日已批订单闸门自己记）
│   └─ 两阶段：pre_preview → 券商 preview → post_preview（回执绑定+一次性）
│   ▼ can_submit=true
├─ Codex 提交与 approved_intent 逐字段一致的订单 → 按订单 ID 验证至终态
│   ▼
└─ journal_append.py（链式日志）→ behavior_review.py（行为画像）
```

## 安装

```bash
git clone https://github.com/Rroyyyyyyyyyyyy/agentic-trading-skills.git
cp -R agentic-trading-skills/skills/codex-agentic-trader ~/.codex/skills/
python3 ~/.codex/skills/codex-agentic-trader/scripts/test_codex_trader.py   # 44 项全绿
python3 ~/.codex/skills/codex-agentic-trader/scripts/test_desk.py           # 48 项全绿
```

### 激活实盘（缺一不可，详见 skill 内 SKILL.md）

1. 改 `policy/policy.json`：`account_last4` 换成你账户末四位，资金/仓位参数按需调整；
2. Codex 完成券商 MCP OAuth（按 Robinhood Agentic Trading MCP 设计），`enabled_tools` 白名单锁死工具面，`runtime_scope_gate_live.py` 验证；
3. **shadow 试运行**（默认态，建议 ≥10 个交易日）：验证对账/闸门/日报，零真实下单；
4. 你本人在终端签发 mandate（唯一实盘开关）：

```bash
python3 ~/.codex/skills/codex-agentic-trader/scripts/mandate_admin.py create --days 30 --max-order 600 --max-daily-turnover 1500
```

5. 按 `references/automation-prompt.md` 在 Codex 建每日 automation（美东 8:00–17:30，盘中每 30 分钟决策循环 + 收盘复盘日报）。

### 紧急停止（任一立即生效）

```bash
touch ~/.codex-agentic-trader/HALT
```

```bash
python3 ~/.codex/skills/codex-agentic-trader/scripts/mandate_admin.py revoke
```

## 快速体验（不连券商也能玩）

```bash
python3 skills/codex-agentic-trader/scripts/decision_gate.py --input examples/demo-decision.json
```

把回撤调到 30%、仓位怼超上限、挑一只 $3 仙股、伪造一张不匹配的 preview 回执——看它逐条拒绝，就理解这套东西的脾气了。

## 目录结构

```
skills/codex-agentic-trader/
├── SKILL.md                        # 固定运行序 + 模式推导 + 激活步骤
├── references/
│   ├── hard-boundaries.md          # 硬边界（只有账户所有人能改）
│   ├── execution-contract.md       # 两阶段执行契约 + mandate 契约 + 状态账本
│   ├── decision-card.md            # 基础决策 JSON 契约与决策卡格式
│   ├── research-playbook.md        # 每日研判顺序 / 候选漏斗 / 行为偏差速查
│   └── automation-prompt.md        # Codex 每日 automation 模板
├── scripts/
│   ├── live_gate.py                # 实盘两阶段闸门（主闸）
│   ├── decision_gate.py            # 基础确定性检查层
│   ├── mandate_admin.py            # mandate 签发/撤销/状态（仅人可用）
│   ├── runtime_scope_gate_live.py  # MCP 工具面白名单闸门
│   ├── journal_append.py           # 链式日志
│   ├── behavior_review.py          # 行为画像
│   └── test_codex_trader.py / test_desk.py   # 92 项回归测试
└── policy/                         # policy.json + MCP 工具白名单
```

## 常见问题

**Q：为什么选 Codex 而不是 Claude？**
A：Anthropic 给 Claude 内置了不可解除的禁令——任何情况下不执行金融交易（用户授权也不行）。全自动执行只能跑在没有这条禁令的 agent 上；本项目适配了 Codex。（用 Claude 的话，它可以跑本 skill 的 shadow 模式当研究员，扣扳机永远是你。）

**Q：mandate 到底防什么？**
A：防 AI 自我授权与授权漂移：签发必须人在终端输入完整确认语；文件权限 0600 + Keychain 校验和，被改动即失效；到期自动降级 shadow；额度与 policy 取更严。它不防有完整 shell 控制权的对抗者——所以别拿它跑你亏不起的钱。

**Q：数据从哪来？**
A：skill 不绑数据源。行情用宿主可用的市场 MCP（如 Robinhood 官方 Agentic MCP 只读行情工具），新闻用 Web 检索一手来源；闸门只认新鲜数据（默认 ≤300 秒），行情过期直接拒绝。

**Q：策略参数通用吗？**
A：默认按约 $5,000 实验账户设定（双 ETF 核心 + 最多 2 只个股卫星）。全部参数在 `policy.json`，按你的规模和风险偏好改——改完先跑 92 项测试再上线。

## 致谢

- [HKUDS/Vibe-Trading](https://github.com/HKUDS/Vibe-Trading)：mandate 授权制、HALT 哨兵、行为画像思想
- [tradermonty/claude-trading-skills](https://github.com/tradermonty/claude-trading-skills)：结构化分析与复盘的定位
- 对账与状态机模式参考 nautilus_trader 与 Lean 的公开设计

## 免责声明

本项目全部输出为规则化研究产物与个人自动化工具，**不构成投资建议**；作者与贡献者不对使用本项目产生的任何盈亏负责。签发 mandate 即代表你理解两阶段闸门的能力边界，并自愿承担全部执行风险。

## License

MIT
