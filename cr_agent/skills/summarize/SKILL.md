# summarize_report

## 目的
将已收集的上下文和各维度专家报告聚合为最终代码评审内容。

## 何时使用
在 `dimension_review` 之后调用此 skill。如果校验失败，携带 validation errors 再次调用。

## 输入
- 已收集的上下文
- 维度专家报告与规范化 findings
- 原始 diff
- 标题与描述
- commit messages
- 可用时的上一轮评审报告
- 可用时的需求文档路径或内容引用
- 如存在，上一轮尝试产生的 validation errors
- `prompt/summary.md`
- `prompt/rules/summaryRule.md`

## 工具
summary subagent 不得使用任何工具。

## 执行契约
- 必须结合 `prompt/summary.md` 和 `prompt/rules/summaryRule.md` 生成最终评审内容。
- 当 `validation_errors` 非空时，只修复上一轮输出结构问题，不扩大评审范围。

## 输出
summary subagent 必须严格返回 JSON，不要使用 Markdown 代码围栏，不要在 JSON 前后追加任何说明文字。JSON 字段为：
- `llm_result`
- `line_comments`
- `issues`

`status`、`log_path`、`tokens_consume` 等运行时字段由 orchestrator 补充。

## 重试契约
当 `validation_errors` 非空时，只修复上一轮输出结构问题，不扩大评审范围。

## 失败策略
空输出或无效输出由 validation 和 main-agent retry 处理。
