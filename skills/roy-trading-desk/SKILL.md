---
name: roy-trading-desk
description: Roy 的个人交易研究台。用于市场研判、候选研究、决策卡生成、手动成交记录、行为偏差复盘和策略进化评估。券商无关、永不下单——AI 只出决策卡，执行永远由 Roy 本人在券商 App 手动完成。任何模式下本 skill 都不是交易授权。
---

# Roy Trading Desk（个人交易研究台）

融合三个来源的设计：robinhood-agentic-trader 的确定性闸门与 fail-closed 纪律、HKUDS Vibe-Trading 的"授权不可达"与行为画像思想（并补上它缺的全局回撤门）、tradermonty 系的"只分析不下单"定位。

## 铁律（每次调用先读，不可覆盖）

1. **AI 永不执行交易**：本 skill 无任何下单/撤单/预览/转账能力，也永远不得新增。所有操作产出的终点是**给 Roy 的手动执行决策卡**。这是结构性边界，不是配置项。
2. **闸门在 LLM 之外**：能否"建议执行"由 `scripts/decision_gate.py` 确定性判定；LLM 可以研究、排序、写论点，**不得**计算最终敞口上限、不得解释绕过任何一条失败的闸门。
3. **fail-closed**：数据缺失不猜、行情过期不用、闸门报错即停、日志追加失败即停。
4. **策略参数只有 Roy 能改**：`policy/policy.json` 是唯一参数源；AI 只能提改动建议（走 evolve 模式），不得直接修改。
5. **凭据纪律**：完整账户号、凭据、token 永不进入本 skill 的任何输入、输出、日志。
6. 必读 [hard-boundaries.md](references/hard-boundaries.md)；做决策时加读 [decision-card.md](references/decision-card.md)；做研究时加读 [research-playbook.md](references/research-playbook.md)。
7. 本 skill 输出为规则化研究产物，不构成持牌投资建议；盈亏责任由 Roy 本人承担。

## 模式（每次显式指定，不得从上一轮推断）

### mode=research（市场研判与候选研究）
1. 解析当前美东时间与交易日状态。
2. 按 [research-playbook.md](references/research-playbook.md) 执行：市场状态信号（基准 ETF 的 6 个月回报 + 200 日均线，收盘确认）→ 宏观与新闻（一手来源优先，附链接）→ 现有持仓逐个复核（即使不在异动扫描里）→ 候选股量化初筛。
3. 产出：论点 / 反论点 / 失效条件 / 来源链接 / 建议触发器。研究结论**不是**决策；进入执行讨论必须转 mode=decide。

### mode=decide（生成决策卡）
1. 向 Roy 索取或从可用只读渠道获取**新鲜账户状态**（净值、已结算现金、持仓、当日已执行笔数与换手）。拿不到完整状态 → 停，不出卡。
2. 组装决策 JSON（契约见 [decision-card.md](references/decision-card.md)），运行：
   ```bash
   python3 scripts/decision_gate.py --input decision.json
   ```
3. `would_allow=true` → 按闸门返回的 `manual_card` 输出决策卡给 Roy；`would_allow=false` → 原样报告违规项，**不得**修饰、不得建议"变通执行"。
4. 卡内必含失效条件（价格/时间/消息），过期卡作废需重跑。
5. 每张卡（无论放行与否）都用 `journal_append.py` 记档。

### mode=record（登记 Roy 的手动成交）
1. Roy 报成交（或"没执行"）后，逐笔追加：
   ```bash
   python3 scripts/journal_append.py --log <私有日志路径> --input fill.json
   ```
2. 与最近的决策卡核对：偏离（改量/改标的/未按卡执行）如实记 `deviation` 字段，不评判但必须记录。
3. 追加失败 → 停止并报告，不得跳过记账继续干活。

### mode=review（复盘与行为画像）
1. 每日/每周复盘：当日成交核对、组合权重 vs 目标、回撤水位、下一步触发条件。
2. 行为画像（Vibe-Trading shadow-account 思想）：
   ```bash
   python3 scripts/behavior_review.py --log <私有日志路径>
   ```
   检测过度交易、处置效应（卖盈太快/留亏太久）、追涨买入、决策卡偏离率；产出 if-then 改进建议。
3. 建议只作呈现，采纳与否由 Roy 决定；采纳的建议走 evolve 流程进 policy。

### mode=evolve（策略与 skill 进化）
1. 证据来源：本地 journal 统计、复盘发现、上游开源项目值得借的模式（借模式不借代码依赖，star 数不是安全信号）。
2. 每次最多一个边界明确的提案：预期收益 / 反证 / 影响的 policy 字段 / 回归测试 / 回滚方式。
3. 改 `policy.json` 或本 skill 的任何文件都需要 Roy 明确批准后由 Roy（或 Roy 授意的会话）执行；**铁律章节与 hard-boundaries.md 只能 Roy 亲自改**。
4. 提案与批准结果记入 journal。

## 数据来源约定

- 行情/基本面：优先可用的市场只读 MCP；没有则用 WebSearch/WebFetch 查一手来源（交易所、发行商、SEC、公司 IR），并标注数据时点。绝不使用过期超过 policy `max_data_age_seconds` 的数据做决策。
- 账户状态：Roy 口述或截图、或任何**只读**渠道。本 skill 不接任何可写券商接口。
- 新闻：Fed/BLS/SEC/公司公告优先；新闻可否决或延迟一笔交易，不能单独创造一笔交易。

## 回归测试

改动任何脚本后必须跑：

```bash
python3 scripts/test_desk.py
```

## 失败策略

- 行情、账户、日历任一不可得：不出决策卡，只做定性研究并明示缺口。
- 闸门脚本本身报错（非违规而是异常）：视为拒绝，报告原始错误。
- 同日重复决策：decision_key 幂等去重，重复即拒。
- Roy 未回报成交结果时：下一张决策卡前先催记账（对账未完成 → 新卡只出观察版，不出执行版）。
