# summarize_report

## 目的
将已收集的上下文和各维度专家报告聚合为最终代码评审内容。

## 何时使用
在 `dimension_review` 之后调用此 skill。如果校验失败，携带 validation errors 再次调用。

## 输入
- 已收集的上下文
- 维度专家报告与规范化 findings
- 如存在，上一轮尝试产生的 validation errors
- `prompt/summary.md`
- `prompt/rules/summaryRule.md`

## 工具
summary subagent 不得使用任何工具。

## 输出
返回包含以下字段的 JSON 内容：
- `llm_result`
- `line_comments`
- `issues`

`status`、`log_path`、`tokens_consume` 等运行时字段由 orchestrator 补充。

## 重试契约
当 `validation_errors` 非空时，只修复上一轮输出结构问题，不扩大评审范围。

## 失败策略
空输出或无效输出由 validation 和 main-agent retry 处理。
