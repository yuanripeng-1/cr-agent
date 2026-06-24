# summarize_report

## 目的
对各维度评审 findings 按 `summaryRule.md` 评级，并输出最终 JSON 报告。

## 何时使用
在 `dimension_review` 之后调用。校验失败时携带 validation errors 重试。

## 输入
- 各维度评审 findings（JSON 数组，已由 Python 预过滤低分项）
- `prompt/summary.md`
- `prompt/rules/summaryRule.md`
- 如存在，上一轮 validation errors

## 工具
summary subagent 不得使用任何工具。

## 执行契约
- 只做评级与格式化输出，不重新审查代码、不注入额外上下文。
- 结合 `prompt/summary.md` 与 `prompt/rules/summaryRule.md` 生成 JSON。
- `validation_errors` 非空时，只修复结构问题，不扩大评审范围。

## 输出
严格返回 JSON（无围栏、无前后说明），字段：
- `llm_result`
- `line_comments`
- `issues`

`status`、`log_path`、`tokens_consume` 由 orchestrator 补充。

## 失败策略
空输出或无效输出由 validation 和 main-agent retry 处理。
