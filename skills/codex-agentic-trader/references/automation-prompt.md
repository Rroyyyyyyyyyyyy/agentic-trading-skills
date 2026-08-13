# Automation 模板

默认只创建 research/shadow automation。当前 `supervised_review` 需要在 Robinhood review 之后获得逐笔明确确认，因此不得将定时器写成“无人值守自动下单”。

```text
每次唤醒使用 $codex-agentic-trader。规则冲突时以 references/hard-boundaries.md 取更严者。

【模式】
- 默认 research/shadow；不调 review_equity_order/place_equity_order/cancel_equity_order。
- 只有账户所有人已单独启用 supervised_review，并且当次任务有人可以查看 review 与确认时，才允许停在 can_request_confirmation 等待。无确认就不提交。

【美东时段】
- 08:00：只检查连接/交易日历，无异常保持安静。
- 08:30：完整盘前研判。不下单。
- 09:30-16:00 每 30 分钟：刷新账户、持仓、全部挂单/当日成交、行情、新闻、财报和风险；有效信号才出新决策。无变化不交易。新个股风险不早于 10:00。
- 16:30/17:00：只对账，不 review/下单。
- 17:30：收盘复盘、基准对比、次日条件和最多一个进化提案。
- 周末/休市：不交易；只在收盘复盘时生成一次休市报告。

【通知】
- 通知渠道、收件人和幂等 UUID 由安装者单独配置，不写入公开 Skill。
- 盘中只在成交、取消、异常订单、风险触发、契约漂移或连接受阻时通知。
- 数据获取失败写“无法获取”，不写成 0。

【进化】
- 只写证据、反证、候选补丁、新增测试、shadow 指标和回滚方案。
- 不自动改 active Skill、policy、mandate、账户绑定、automation 或风险上限。
```

安装者必须在外部运行配置中定义通知收件人、时区和数据保存目录。不得将个人 userId、完整账号或凭据提交到公开仓库。
