# validate_json

## 目的
校验最终评审结果是否能被下游系统消费。

## 何时使用
每次 `summarize_report` 尝试之后立即调用。

## Agent 策略
此 skill 是确定性的 Python 代码。不得调用 agent，也不得使用工具。

## 校验范围
- `ReviewResult` schema
- `line_comments.comments` and `issues.locations` alignment
- 相对文件路径
- 正数行范围
- diff 上下文可用时的新增行范围

## 输出
返回 `ValidationResult(valid, errors)`。

## 失败策略
绝不修改 report。返回 validation errors，由 main agent 决定是否重试 summary。
