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
每个 dimension subagent 只能使用 `ToolFacade.tools_for("dimension")` 提供的工具。

## 执行契约
- 按 platform 选择审查维度。
- 使用 `defaults.concurrency` 和 `asyncio.Semaphore` 控制并发。
- 每个维度必须隔离运行，使用独立的 prompt 和 SDK options。
- 每个维度写入 `dimensions/<dimension>.json`。
- 所有维度结束后，skill 写入 `dimensions/manifest.json`。

## Subagent 输出
dimension subagent 必须返回 YAML 格式的专家报告文本，不要使用 Markdown 代码围栏包裹。

## 适配器职责
`skill.py` 必须解析 YAML，保留 `raw_yaml`，规范化 findings，并写入 JSON 产物。

## 失败策略
单个维度失败不能导致整个任务失败；如果所有维度都失败，则抛出错误。
