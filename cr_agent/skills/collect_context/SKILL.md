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
- `semble_search` 可用且有助于语义上下文时再使用：参数 `{query, top_k}`，`top_k` 建议 3-5，按相关度返回代码片段。
- 调用图（CRG）类工具按需、克制使用，控制喂回的上下文体积：
  - `crg_query {query=<base_sha>}`：先看本次 MR 改了哪些符号/风险/测试缺口（变更摘要，非调用链）。
  - `crg_callers {target}` / `crg_callees {target}`：拿关键符号的**直接调用方/被调方（仅一层）**。`target` 用 `path::funcName`（如 `frontend/src/app/page.tsx::DashboardContent`）。**只查一层，不要逐层展开调用链**；必要时用 `limit`（默认 20）进一步收窄。
  - `crg_affected_flows {base?, limit?}`：变更影响了哪些执行流。
  - `crg_get_flow {flow_name|flow_id, limit?}`：需要看完整一条业务路径时再用。
  - 原则：优先 1-2 个关键符号的一层调用关系即可，避免一次拉取过多节点把上下文撑爆。
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
