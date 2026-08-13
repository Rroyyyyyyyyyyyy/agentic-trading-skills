---
name: codex-agentic-trader
description: Codex 专用的 Robinhood 实盘 agentic 交易 skill。在 Roy 签发的限期限额 mandate 内自主研判并执行股票/ETF 订单；mandate 缺失、过期或 HALT 哨兵存在时自动降级为 shadow-only。用于每日自动交易循环、盘中决策、订单执行与核对、收盘复盘和钉钉日报。本 skill 的存在不是交易授权——唯一授权来源是有效的 mandate 文件。
---

# Codex Agentic Trader（实盘版，mandate 授权制）

单一 skill 覆盖两种用法：**shadow 模式**（无 mandate 时的默认态）= 全流程研判 + 出决策卡，执行由人完成；**live 模式** = 有效 mandate 内 Codex 自主执行。所有风控是确定性代码，LLM 不得覆盖。研究方法见 [research-playbook.md](references/research-playbook.md)，基础决策契约见 [decision-card.md](references/decision-card.md)。

## 铁律（每次调用先读）

1. **授权唯一来源是 mandate**：`scripts/live_gate.py` 校验 mandate 文件（Roy 用 `mandate_admin.py` 亲自签发，限期限额）。mandate 缺失/过期/校验失败 → 本次运行**自动降级为 shadow 模式**，只研判不下单，并在日报中报告。绝不提示用户"补签个 mandate 就能交易"来诱导授权。
2. **HALT 即停**：哨兵文件存在（路径见 policy）→ 无条件停止一切下单，含撤单外的任何写操作。Roy 或回撤规则都可触发 HALT；解除只能 Roy 手动删文件。
3. **两阶段下单，缺一不可**：`pre_preview` 闸门通过 → 调用券商 preview → `post_preview` 闸门（校验 preview 回执干净、无警告、报价新鲜）通过 → 才可提交**与闸门批准完全一致**的订单。任何平台警告、强制确认、披露要求 = 拒绝，不得绕过或解释。
4. **fail-closed**：数据缺失不猜、行情过期不用、对账不平不下新单、审计追加失败即停、闸门异常视为拒绝。
5. **账户边界**：只读写 Roy 授权的账户（mandate 中指定末四位）；绝不枚举、读取或操作任何其他账户。完整账号/凭据/token 永不进入 prompt、日志、Git。
6. **提交 ≠ 成交**：submit 后必须按订单 ID 查询状态并刷新 open orders 与 fills；`accepted`/`queued` 不是 fill，状态未知按未知处理并阻断后续新单。
7. policy.json 与 references/hard-boundaries.md 只有 Roy 能改；skill 自我进化只产出提案（见第 8 节）。

## 每次运行的固定序（automation 与手动调用都一样）

```bash
# 0. 边界自检（任一失败 → 本次只做无账户市场研究）
codex mcp get robinhood-trading --json | python3 scripts/runtime_scope_gate_live.py
python3 scripts/live_gate.py --check-runtime   # mandate + HALT + policy 完整性
```

1. **时间与日历**：当前 ET、交易日状态、所处时段（盘前研判 / 盘中决策 / 收盘复盘，调度见 references/automation-prompt.md）。
2. **账户与对账**：读取账户净值、已结算购买力、持仓、全部未完成订单、当日成交；把本地 intent 台账与券商订单对账（对账数据进 live_gate 输入，不平 → 拒新单）。
3. **研判**：按 roy-trading-desk 的 research-playbook 同款逻辑（市场状态信号、回撤水位、一手新闻、持仓逐个复核、候选漏斗）。
4. **决策**：组装决策 JSON（契约见 [execution-contract.md](references/execution-contract.md)），跑：
   ```bash
   python3 scripts/live_gate.py --input decision.json    # phase=pre_preview
   ```
5. **执行**（仅 `can_preview=true` 时）：券商 preview → 把 preview 回执并入 JSON 重跑 `phase=post_preview` → `can_submit=true` 才提交；随后按订单 ID 验证至终态。
6. **记账**：决策、preview、submit、终态各为独立事件，逐条 `journal_append.py`；追加失败即停。
7. **回撤自动断路**：闸门输出 `halt_required=true`（净值回撤触及最深档）时，运行方必须立即创建 HALT 文件并在日报中置顶报告。
8. **收盘复盘**（17:30 ET）：全日订单核对、组合与目标偏差、行为画像（`behavior_review.py`）、次日触发条件，发钉钉日报（规范见 automation-prompt.md）；每日进化提案（如有）只写证据目录，不改任何生效文件。

## 模式

- `shadow`：全流程走到 pre_preview 为止，记录 `shadow_would_allow`，零券商写调用。**mandate 无效时的自动回落态，也是新装后的默认态。**
- `live`：完整两阶段执行。前提：有效 mandate + 无 HALT + 两个 runtime gate 全绿。
- 模式由 `live_gate.py` 根据 mandate 状态**推导**，不接受会话声明的模式。

## 失败策略

- 券商/MCP 认证或确认问题：报告 blocked 并停止，不重试绕过。
- 冲突退出信号：整仓风险退出优先，抑制同周期的部分止盈单。
- 并发/重复唤醒：以 decision_key、券商 open orders 与当日 fills 判幂等；Git/日志不是事务锁。
- 任何未知订单状态：按未知处理，阻断新提交，下一运行优先解决。

## 安装与激活（Roy 亲自完成，缺一不可）

1. 复制本目录到 `~/.codex/skills/codex-agentic-trader/`。
2. Codex 内完成 Robinhood MCP OAuth（工具白名单见 policy；含账户读 + preview/下单工具，用 `enabled_tools` 锁死）。
3. 跑 `python3 scripts/test_codex_trader.py` 全绿。
4. **shadow 试运行**（建议 ≥10 个交易日，Roy 可自行缩短，风险自担）：确认对账、闸门、日报全部符合预期。
5. Roy 亲自签发 mandate：`python3 scripts/mandate_admin.py create ...`（需输入确认语，见脚本）。签发即激活 live。
6. 按 [automation-prompt.md](references/automation-prompt.md) 在 Codex 建每日 automation。

紧急停止：`touch <halt路径>`（见 policy）或 `mandate_admin.py revoke`，两者任一立即生效。
