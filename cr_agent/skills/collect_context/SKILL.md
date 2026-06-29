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
- 先理解原始 diff。
- 提取变更文件和新增行范围。
- 需要具体证据时，默认先用 `grep_text` 定位符号或关键词，再用 `read_file_range` 读取最小必要片段。
- `read_file` 只能作为例外使用：文件很小、无法预判行号、或必须理解文件级结构时才可调用。禁止为了方便读取整文件；读取内容必须服务于 `semantic_context`、`call_graph_context` 或 `code_snippets`。
- 有助于定位引用时使用 `grep_text`。
- 接口实现反查：当 diff 新增代码调用 repository/storage/cache 接口方法，且该返回值进入对外状态字段、决策分支或错误映射时，必须用 `read_file_range` 读取该接口方法的具体实现（SQL 条件、adapter 空值语义、缓存写入方的 value 与 TTL）。仅读取接口签名不算完成。反查到的实现位于 diff 之外，只能作为下游维度的推理证据；收集时必须同时保留其**对应 diff 调用点的位置**，供维度据此锚定 finding。
- 既有契约对照：当新增聚合/门禁/状态接口、或在 README/API 文档相邻行出现既有同名或等价接口时，必须用 `grep_text` 定位并读取既有接口的 handler/service 实现作为契约对照物（同样保留对应 diff 调用点位置）。
- **Semble 语义检索（触发式必须）**：满足以下任一条件时，**必须**至少调用 1 次 `semble_search`（`{query, top_k}`，`top_k` 3-5），不得因 diff 已含新文件就跳过：
  - diff 新增/修改对外接口、入口函数、路由/handler、鉴权或权限相关逻辑；
  - 需要对照项目中「同类接口/同类实现」的既有惯例（鉴权、参数校验、错误处理、跨域等），但不知道确切符号名、grep 难以快速定位。
  `query` 必须由「本次 diff 的意图」概括而来（改了什么、要对照什么惯例），而非泛词。
- **Semble 相关性闸门（宁缺毋滥）**：semble 返回的是语义近似结果，可能与本次 diff 无关。必须逐条判断相关性，**只**把与本次 diff 的变更文件/符号/意图明确相关的片段写入 `semantic_context`，并对每条给出 `why_relevant`（或 `related_changed_files`）；判断不相关的片段必须丢弃；若全部不相关，`semantic_context` 留空并在 `warnings` 记一条「semble 命中均与 diff 无关，已丢弃」。错误或无关的上下文比缺失更有害。
- **CRG 调用图（触发式必须）**：当 CRG 工具可用，且 diff 涉及「入口注册 / 新增对外接口或公共函数 / 跨层调用（如 接口层→服务层/存储层）」时，**必须**按序至少执行：
  1. `crg_affected_flows {base?, limit?}`：本次变更影响了哪些执行流；
  2. 对 1-2 个关键符号各调用一次 `crg_callers` 或 `crg_callees`（**仅一层**，带 `limit`）。关键符号＝diff 中新增/修改的入口或被跨层调用的函数，`target` 用 `path::funcName` 形式（形如 `<相对路径>::<函数名>`，按本次 diff 的实际符号填写，不要照抄示例）。
  另可按需：`crg_query {query=<base_sha>}` 看变更摘要（非调用链）、`crg_get_flow {flow_name|flow_id, limit?}` 看完整业务路径。**只查一层、用 `limit` 收窄**，避免一次拉取过多节点把上下文撑爆。
- **CRG 构建时机**：review 启动时 CRG 已在后台 build/update；优先用 `crg_status` 与 `crg_affected_flows` 等查询工具。仅当 `crg_status.ready=false` 且确需图数据时，才调用 `crg_build_or_update {}` 并等待完成。
- **来源纯洁性**：`call_graph_context` 只能填 `crg_*` 工具返回的摘要；`semantic_context` 只能填 `semble_search` 命中并经相关性闸门保留的内容。**禁止**用 diff 推断后手写调用链、或把 `read_file`/`grep_text` 结果塞进这两个字段冒充。若 CRG/Semble 不可用或失败，写入 `warnings` 并把对应字段留空（不得伪造）。
- **返回前自检**：输出 JSON 前必须自检——(a) 若触发了 Semble/CRG 条件却未调用对应工具，必须先补调再输出；(b) `call_graph_context`/`semantic_context` 是否仅含合法来源；(c) 最终输出是否为纯 JSON。
- 工具失败必须记录为 warnings，不能中断评审。

## Subagent 输出
context subagent 必须严格返回 JSON：最终回复第一个非空字符必须是 `{`、最后一个非空字符必须是 `}`；禁止使用 Markdown 代码围栏（```/```json），禁止在 JSON 前后追加任何说明文字。JSON 字段为：
- `summary`
- `diff_summary`
- `semantic_context`
- `call_graph_context`
- `code_snippets`
- `warnings`

## Artifact
skill 写入 `collected_context.json`。产物必须包含原始 diff 引用、变更文件、语义上下文、调用图上下文、代码片段和 warnings。默认只写瘦身后的 `tool_evidence_summary`；仅当 `[debug].full_tool_evidence=true` 时写完整 `tool_evidence`。

## skill.py 返回
skill handler 返回下游 agent 使用的紧凑结构化上下文。不要返回 `tool_evidence`；工具证据只保留在 `collected_context.json` 中用于审计和 debug，默认以 `tool_evidence_summary` 形式保存。

返回字段：
- `task_id`
- `artifact_path`
- `summary`
- `diff_summary`
- `changed_files`，其中新增行必须压缩为 `added_ranges`，用于维度审查和最终评论定位。
- `semantic_context`
- `call_graph_context`
- `code_snippets`
- `warnings`
- `usage`

## 失败策略
单个工具失败必须降级为 warnings，并基于已有证据继续。context subagent 运行失败时，skill.py 使用本地 diff 摘要写入降级版 `collected_context.json`，并把失败原因加入 warnings。
