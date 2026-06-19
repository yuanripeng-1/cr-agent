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
每个 dimension subagent 只能使用 `ToolFacade.tools_for("dimension")` 提供的工具。默认只提供 `read_file`、`read_file_range`、`grep_text`。

调用 `read_file`、`read_file_range`、`grep_text` 时，`path` 必须是相对 `project_root` 的路径，例如 `frontend/src/app/page.tsx`。不要带 `workspace/.../project_code` 前缀，也不要使用绝对路径。

dimension subagent 不使用 Semble 或 CRG 工具；语义上下文和调用图上下文由 `collect_context` 提供。

## 执行契约
- 按 platform 选择审查维度。
- 使用 `defaults.concurrency` 和 `asyncio.Semaphore` 控制并发。
- 每个维度必须隔离运行，使用独立的 prompt 和 SDK options。
- dimension subagent 必须遵循当前维度的 `prompt/<dimension>.md` 和 `prompt/rules/<dimension>Rule.md`。
- 输入中的 `collected_context` 是 compact 结构化上下文，不是要求 subagent 读取 `collected_context.json`。
- 消费上下文时优先使用 `changed_files.added_ranges` 和 `diff_summary`，再结合 `code_snippets`、`semantic_context`、`call_graph_context`。
- 工具只用于少量补充证据或核实上下文，不要重复执行 context agent 已完成的语义检索或调用图探索。
- 每个维度写入 `dimensions/<dimension>.json`。
- 所有维度结束后，skill 写入 `dimensions/manifest.json`。

## Subagent 输出
dimension subagent 必须只返回有效 YAML。不要使用 Markdown 代码围栏，不要在 YAML 前后追加任何说明文字。

## 适配器职责
`skill.py` 必须解析 YAML，保留 `raw_yaml`，规范化 findings，并写入 JSON 产物。

## 失败策略
单个维度失败不能导致整个任务失败；如果所有维度都失败，则抛出错误。
