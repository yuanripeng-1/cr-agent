# dimension_review

## 目的
运行配置中的审查维度，并为汇总阶段生成专家报告。

## 何时使用
在 `collect_context` 完成后调用此 skill。

## 输入
- 已收集的上下文
- `config/dimensions.toml`
- `prompt/<dimension>.md`
- `prompt/rules/<dimension>Rule.md`

## 工具
每个 dimension subagent 只能使用 `ToolFacade.tools_for("dimension")` 提供的工具。默认只提供 `read_file_range`、`grep_text`，不提供 `read_file`。

调用 `read_file_range`、`grep_text` 时，`path` 必须是相对 `project_root` 的路径，例如 `frontend/src/app/page.tsx`。不要带 `workspace/.../project_code` 前缀，也不要使用绝对路径。

dimension subagent 不使用 Semble 或 CRG 工具；语义上下文和调用图上下文由 `collect_context` 提供。

## 执行契约
- 按 platform 选择审查维度。
- 使用 `defaults.concurrency` 和 `asyncio.Semaphore` 控制并发。
- 每个维度必须隔离运行，使用独立的 prompt 和 SDK options。
- dimension subagent 必须遵循当前维度的 `prompt/<dimension>.md` 和 `prompt/rules/<dimension>Rule.md`。
- 输入中的 `collected_context` 是 compact 结构化上下文，不是要求 subagent 读取 `collected_context.json`。
- 消费上下文时优先使用 `changed_files.added_ranges` 和 `diff_summary`，再结合 `code_snippets`、`semantic_context`、`call_graph_context`。
- 工具用于少量补充证据或核实上下文。若新增代码依赖被调函数语义、共享状态读取端或既有契约，而 `collected_context` 未包含对应实现，必须用 `grep_text` / `read_file_range` 补齐该 callee 或读取端实现；仅在 `collected_context` 已明确提供该证据时才不重复检索。补查到的实现位于 diff 之外，只能作为 analysis/evidence，finding 仍须锚定到 diff 内调用点行。
- 每个维度写入 `dimensions/<dimension>.json`。
- 所有维度结束后，skill 写入 `dimensions/manifest.json`。

## Subagent 输出
dimension subagent 必须只返回有效 YAML。不要使用 Markdown 代码围栏，不要在 YAML 前后追加任何说明文字。

## 输出约束
- 默认每个维度最多输出 3 个 high-confidence findings。
- 如果存在 critical / security / data-loss / merge-blocking 级别问题，可以超过 3 个，但每个问题必须有明确 diff 内锚点。
- 没有明确、可定位、高置信问题时，输出空 findings，不要输出长篇分析。
- 每个 finding 的 `analysis`、`evidence`、`suggestion` / `code_suggestion` 使用短段落；只写根因、证据和可执行修复，不写审查过程。
- 禁止输出低置信、重复、泛泛建议；输出限制不是忽略关键问题，而是过滤不能可靠落到 diff 行的问题。

## 适配器职责
`skill.py` 必须解析 YAML，保留 `raw_yaml`，规范化 findings，并写入 JSON 产物。

## 失败策略
单个维度失败不能导致整个任务失败；如果所有维度都失败，则抛出错误。
