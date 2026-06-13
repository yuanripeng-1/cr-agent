# CR-Agent (Claude Agent SDK 重构版)

当前仓库处于**完全重构阶段**，已完成 M0 脚手架。

## 运行入口

- 启动脚本：`RUN.sh`
- Python 入口：`cr_agent/main.py`
- 启动命令：

```bash
bash RUN.sh --config workspace/<task>/agent_config.toml [--platform gitlab|infcode]
```

## 平台识别规则（严格）

仅支持两种来源，优先级如下：

1. `--platform`
2. `workspace/config.toml` 中的 `platform`

若都缺失则启动失败。

## M0 已落地内容

- 新目录骨架：`cr_agent/`、`config/`
- 输入模型：`cr_agent/core/review_input.py`
- 启动装配：`cr_agent/bootstrap.py`
- 占位技能目录：`collect_context`、`dimension_review`、`summarize`、`validate_json`
- 占位工具外观层：`cr_agent/tools/`
- `context.json` 契约补充：`commit_messages`
- 外部契约冻结：`agent_config.toml`、`context.json`、最终 `result.json`
- 外部契约字段说明：`cr_agent/schemas/agent_config_schema.json`、`context_schema.json`、`summary_schema.json`
- 契约校验入口：`python -m cr_agent.core.verify_contracts`

## 外部契约冻结

> 状态:已冻结输入配置、上下文输入、最终输出三类外部契约。
> 范围:只约束后端/启动脚本与 CR-Agent 的边界,不约束 skill 之间的中间数据结构。

### 1. 运行配置契约:agent_config.toml

样例:`workspace/764-ef5c5c99/agent_config.toml`

#### 1.1 必填字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `[context].json_path` | string | `context.json` 路径,可相对 `agent_config.toml` 所在目录或当前工作目录 |
| `[llm].model` | string | 当前运行使用的模型标识 |

#### 1.2 可选字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `[context].result_path` | string | 输出目录;缺失时按 task_id 生成兜底目录 |
| `[project].language` | string | 待审查项目语言 |
| `[project].status` | string | 当前项目状态,如 `update` |
| `[project].guidelines_path` | string | 规范文档路径 |
| `[project].requirements_path` | string | 需求文档路径 |
| `[llm].api_key` | string | 模型代理鉴权信息 |
| `[llm].api_base` | string | Anthropic 兼容代理地址 |
| `platform` | `gitlab`/`infcode` | 推荐位置,用于切换工具集与维度 |
| `[llm].platform` | `gitlab`/`infcode` | 兼容历史样例;低于命令行 `--platform` |

平台识别优先级:

1. `RUN.sh --platform <gitlab|infcode>`
2. `agent_config.toml` 根级 `platform`
3. `[llm].platform`
4. 全部缺失则启动失败

实现位置:
- Python 强校验模型:`cr_agent/core/agent_config.py`
- 原格式字段说明:`cr_agent/schemas/agent_config_schema.json`

### 2. 上下文输入契约:context.json

样例:`workspace/764-ef5c5c99/context.json`

#### 2.1 冻结必填字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `task_id` | string | 单次审查任务 ID |
| `title` | string | MR 标题或 commit 总结标题 |
| `diff_content` | string | Git diff 文本 |
| `project_root` | string | 待审查项目目录 |
| `commit_messages` | array[string] | 本次审查范围内的 commit message 列表;兼容历史调用,允许缺失或为空 |

#### 2.2 兼容可选字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `project_id` | number/null | GitLab 项目 ID |
| `mr_iid` | number/null | GitLab MR IID |
| `description` | string | MR 描述或开发者补充说明 |
| `base_sha`/`head_sha`/`start_sha` | string | diff 范围与行号校验信息 |
| `source_branch`/`target_branch` | string | 分支信息 |
| `diff_file_path` | string | diff 文件路径 |
| `previous_report` | string | 上轮审查报告,可用于增量审查 |
| `requirements_Doc` | string | 需求文档路径;代码同时兼容 `requirements_doc` |
| `platform` | `gitlab`/`infcode`/null | 上下文携带的平台标识;启动平台仍以 `agent_config.toml`/`--platform` 为准 |

实现位置:
- Python 强校验模型:`cr_agent/core/review_input.py`
- 原格式字段说明:`cr_agent/schemas/context_schema.json`

### 3. 最终输出契约:result.json

样例:`workspace/cr_result/764-ef5c5c99/result.json`

#### 3.1 冻结必填字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `llm_result` | string | 后端展示/存储的 Markdown 审查报告 |
| `status` | `success`/`failed`/`error`/`bootstrap_ready` | 运行状态 |
| `log_path` | string | 运行日志路径 |
| `tokens_consume` | object | token 与成本统计 |
| `line_comments.comments` | array[object] | 可回写到 GitLab 的行级评论 |
| `issues` | array[object] | 问题摘要列表 |

#### 3.2 tokens_consume

| 字段 | 类型 | 说明 |
|---|---|---|
| `input_tokens` | number | 输入 token 数 |
| `output_tokens` | number | 输出 token 数 |
| `cost` | number | 成本 |

#### 3.3 line_comments.comments[]

| 字段 | 类型 | 说明 |
|---|---|---|
| `new_path` | string | 新文件路径 |
| `body` | string | 评论内容 |
| `start_line` | number | 起始行,从 1 开始 |
| `end_line` | number | 结束行,必须大于等于 `start_line` |

#### 3.4 issues[]

| 字段 | 类型 | 说明 |
|---|---|---|
| `severity` | `critical`/`major`/`minor`/`info` | 严重等级 |
| `title` | string | 问题标题 |
| `count` | number | 同类问题数量 |
| `locations` | array[object] | 位置列表 |

实现位置:
- Python 强校验模型:`cr_agent/core/review_output.py`
- 原格式字段说明:`cr_agent/schemas/summary_schema.json`

说明:`summary_schema.json` 当前沿用历史文件名,但本阶段约束的是最终 `result.json`
外部输出契约,不是 skill 内部 summary 中间结构。

### 4. 校验方式

```bash
python -m cr_agent.core.verify_contracts \
  --config workspace/764-ef5c5c99/agent_config.toml \
  --context workspace/764-ef5c5c99/context.json \
  --result workspace/cr_result/764-ef5c5c99/result.json
```

说明:`cr_agent/schemas/` 下保存的是贴近真实输入/输出格式的字段说明文档,
方便团队直接对照字段含义;运行时强校验由 `cr_agent/core/` 下的 Pydantic 模型负责。

### 5. 暂不冻结的内容

以下内容属于 agent/skill 内部实现细节,本阶段暂不创建契约:

- `collect_context` 输出的上下文结构
- `dimension_review` 的维度评分结构
- `summarize` 输入输出中间结构
- 主 agent 编排状态与 retry state

这些结构应在对应 skill 开发前再冻结,避免过早设计导致后续返工。

## 关于 `config/` 目录

`config/` 下文件是**项目模板/静态策略配置**（后续维度与工具授权会从这里读取），
不是本次任务运行时必须传入的配置文件。

当前运行时仍然以 `workspace/<task>/agent_config.toml` 为准。

## SDK Runtime + 主 Agent 编排测试骨架进展

> 状态:已完成可复用 pytest 骨架与占位编排链路。
> 范围:只保证 bootstrap → orchestrator → 4 个占位 skill → artifacts 的流程可跑通;
> 不调用真实 LLM、不启动 LiteLLM、不调用真实 Claude Agent SDK、不调用真实工具。

### 1. 本次新增/调整的生产代码

#### 1.1 `cr_agent/main.py`

- 原行为:bootstrap 后直接写 `bootstrap_ready` 占位结果。
- 现行为:bootstrap 后调用 `core.orchestrator.run_review(runtime)`。
- 目的:让 CLI 入口接入主编排层,产物写盘统一由 orchestrator/artifacts 负责。

#### 1.2 `cr_agent/core/types.py`

- 定义 runtime 与 orchestrator 共享的轻量类型:
  - `TokenUsage`:任务级 token 汇总,包含 `input_tokens`、`output_tokens`、
    `cache_creation_tokens`、`cache_read_tokens`。
  - `QueryResult`:模型调用结果抽象,测试 fake runtime 与后续真实 runtime 共用。
  - `ValidationResult`:validate skill 的统一返回结构。

#### 1.3 `cr_agent/core/usage.py`

- `accumulate_usage(total, call)`:累计任务级 token。
- `extract_usage(raw_response)`:从 dict 或对象形式的 SDK 响应中容错提取 usage。
- usage 缺失时返回 0,不阻断审查流程。

#### 1.4 `cr_agent/core/errors.py`

- 定义 runtime 标准错误:
  - `RuntimeCallError`
  - `RuntimeTimeoutError`
  - `RuntimeTokenLimitError`
  - `RuntimeContextLimitError`
- 目的:后续 orchestrator 不直接依赖 Claude SDK/LiteLLM/模型厂商原始异常格式。

#### 1.5 `cr_agent/core/sdk_runtime.py`

- 新增 `ClaudeAgentRuntime` 最小适配器骨架。
- 当前支持:
  - `query_main()`
  - `query_subagent()`
- 当前测试通过 mock client 验证响应解析、空结果、超时、token/context limit 错误分类。
- 真实 Claude Agent SDK / LiteLLM 调用尚未接入。

#### 1.6 `cr_agent/core/state.py`

- 新增 `ReviewState`,记录一次审查运行状态:
  - `status`
  - `attempt`
  - `max_retries`
  - `tokens_consume`
  - `errors`
  - `warnings`

#### 1.7 `cr_agent/core/artifacts.py`

- 统一封装产物写盘:
  - `write_result_json()`
  - `write_result_markdown()`
  - `append_run_log()`
- 目的:成功、失败、异常终态都通过同一处写 `result.json`、`cr_result.md`、`run.log`。

#### 1.8 `cr_agent/core/orchestrator.py`

- 新增主编排占位链路:
  1. 可选调用 `agent_runtime.query_main()`。
  2. 调用 `collect_context`。
  3. 调用 `dimension_review`。
  4. 调用 `summarize_report`。
  5. 调用 `validate_json`。
  6. validate 失败时按 `max_retries` 重试 summary。
  7. 写最终 `result.json`、`cr_result.md`、`run.log`。
- 支持依赖注入 `agent_runtime` 与 `skill_registry`,测试时可替换为 fake。

#### 1.9 4 个 skill 占位实现

- `skills/collect_context/skill.py`
  - 新增 async `collect_context(runtime_context)`,返回基础任务上下文。
- `skills/dimension_review/skill.py`
  - 新增 async `dimension_review(collected_context)`,返回 placeholder 维度评分。
- `skills/summarize/skill.py`
  - 新增 async `summarize_report(...)`,返回 placeholder Markdown 报告。
- `skills/validate_json/skill.py`
  - 新增 async `validate_json(report)`,当前只校验 `llm_result` 非空。
  - attempt 不在 validate 内维护,仍归 orchestrator。

#### 1.10 `skills/registry.py`

- 新增 `SkillRegistry`。
- 新增 `build_default_skill_registry()`。
- 新增 `registered_skill_names()`。
- 目的:orchestrator 不直接散落 import 各 skill,测试可替换 fake registry。

#### 1.11 `requirements.txt`

- 新增测试依赖:
  - `pytest`
  - `pytest-asyncio`

### 2. 本次新增测试骨架

#### 2.1 测试辅助

- `tests/conftest.py`
  - 提供临时 per-task `agent_config.toml` 与 `context.json` fixture。
  - 避免测试依赖真实 workspace 路径。
- `tests/fakes.py`
  - `FakeAgentRuntime`:模拟主 agent/runtime 调用。
  - `FakeSkillScenario`:模拟 4 个 skill,支持控制 validate 失败次数。

#### 2.2 测试文件职责

- `tests/test_bootstrap_smoke.py`
  - 验证 `bootstrap_runtime()` 能把 config/context 装配成 `RuntimeContext`。
- `tests/test_orchestrator_smoke.py`
  - 用 fake runtime + fake skills 跑通端到端占位编排。
  - 覆盖成功态、validate 失败后重试、超过 `max_retries` 后终态产物。
- `tests/test_runtime_contract.py`
  - 用 `unittest.mock.AsyncMock` 模拟 SDK client。
  - 覆盖正常返回、空模型结果、超时、token limit、context limit。
- `tests/test_usage.py`
  - 覆盖 usage 提取、缺失 usage 容错、任务级 token 累计。
- `tests/test_artifacts.py`
  - 覆盖 `result.json`、`cr_result.md`、`run.log` 写盘。
- `tests/test_skill_registry.py`
  - 覆盖默认 registry 暴露 4 个 skill,以及占位 skill 链路。
- `tests/test_contracts.py`
  - 已有外部契约测试,继续保留。

### 3. 测试运行方式

首次准备:

```bash
cd /Users/liyu/Desktop/MyCodeEnv/code/crAgent/cr-agent
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

运行全部测试:

```bash
cd /Users/liyu/Desktop/MyCodeEnv/code/crAgent/cr-agent
.venv/bin/python -m pytest -q
```

当前验证结果:

```text
22 passed
```

### 4. 本地任务目录 smoke

当前 `workspace/15-3de6a54a/agent_config.toml` 使用容器路径:

```toml
json_path = "/workspace/15-3de6a54a/context.json"
result_path = "/workspace/cr_result/15-3de6a54a"
```

本机测试时,对应路径是:

```text
/Users/liyu/Desktop/MyCodeEnv/code/crAgent/cr-agent/workspace/15-3de6a54a
```

建议复制临时配置后替换路径:

```bash
cd /Users/liyu/Desktop/MyCodeEnv/code/crAgent/cr-agent

cp workspace/15-3de6a54a/agent_config.toml /private/tmp/cr-agent-15-agent_config.toml

.venv/bin/python -c "from pathlib import Path; p=Path('/private/tmp/cr-agent-15-agent_config.toml'); s=p.read_text(); s=s.replace('/workspace/15-3de6a54a','/Users/liyu/Desktop/MyCodeEnv/code/crAgent/cr-agent/workspace/15-3de6a54a'); s=s.replace('/workspace/cr_result/15-3de6a54a','/Users/liyu/Desktop/MyCodeEnv/code/crAgent/cr-agent/workspace/cr_result/15-3de6a54a'); p.write_text(s)"

.venv/bin/python -m cr_agent.main --config /private/tmp/cr-agent-15-agent_config.toml --platform gitlab
```

产物位置:

```text
workspace/cr_result/15-3de6a54a/result.json
workspace/cr_result/15-3de6a54a/cr_result.md
workspace/cr_result/15-3de6a54a/run.log
```

### 5. 当前仍未实现

- 真实 Claude Agent SDK client 初始化。
- LiteLLM Anthropic-compatible Gateway 的真实调用链路。
- 主 agent 的真实 agentic skill 调用。
- `collect_context` 的真实上下文扩展。
- `dimension_review` 的维度配置读取、并发 subagent 审查。
- `summarize_report` 的真实 summary subagent。
- `validate_json` 的完整 schema 校验。
