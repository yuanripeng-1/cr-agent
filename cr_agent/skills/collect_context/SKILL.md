# collect_context

## 目的
在进入多维度审查前，收集最小且足够的代码评审上下文。

## 何时使用
每次代码评审运行时，第一个调用此 skill。

## 输入
- 来自 `RuntimeContext.review_input` 的 `context.json` 字段
- 原始 diff 内容
- 项目根目录
- 标题与描述
- commit messages
- 可用时的需求文档路径或内容引用
- 可用时的上一轮评审报告

## 工具
context subagent 只能使用 `ToolFacade.tools_for("context")` 提供的工具。

## 执行契约
- 先理解 diff。
- 提取变更文件和新增行范围。
- 需要具体证据时使用 `read_file` 和 `read_file_range`。
- 有助于定位引用时使用 `grep_text`。
- `semble_search` 可用且有助于语义上下文时再使用。
- 只有在调用图上下文有价值且可用时，才使用 `crg_query`。
- 工具失败必须记录为 warnings，不能中断评审。

## 输出
写入 `collected_context.json`，并返回包含以下字段的紧凑摘要：
- `task_id`
- `artifact_path`
- `summary`
- `warnings`
- `usage`

## 失败策略
运行时失败可以使 skill 失败；单个工具失败必须降级为 warnings。
