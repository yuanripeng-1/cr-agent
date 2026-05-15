# CR-Agent 项目说明

CR-Agent 是一个面向 Merge Request / Pull Request 的自动化代码审查工具。它读取一次审查任务的上下文 JSON、代码 diff、需求文档和历史审查结果，然后把同一份 diff 分发给 10 个维度的“专家 Agent”并发分析，最后由 Summary Agent 聚合成 Markdown 审查报告、结构化问题列表和可回写 GitLab 的行级评论。

从代码实现看，它不是 LangChain、AutoGen、CrewAI 这类完整 agent framework 项目，也没有工具规划、记忆、反思、自主多步行动等通用 agent 运行时。它更准确的定位是：基于 LiteLLM 的多角色 Prompt 编排系统。项目内自己实现了 `BaseAgent`、维度 Agent、路由器、并发调度、重试、超时、格式解析、行号校验和结果落盘，因此具备“多 Agent 协作”的工程结构，但核心智能行为主要来自各维度 prompt 和规则文件。

## 目录结构

主工程位于 `cr-agent/`。工作区根目录下的 `data/jobs/...` 更像历史任务产物或被审查项目样本，不是 CR-Agent 的核心运行代码。

```text
cr-agent/
├── agent/
│   ├── main.py          # 当前 RUN.sh 实际调用的主入口
│   ├── run.py           # 旧版/兼容入口，当前 RUN.sh 不调用
│   ├── router.py        # CRRouter：创建 10 个维度 Agent，并发调度和聚合
│   ├── agents.py        # BaseAgent、GenericDimensionAgent、QualityLinterAgent
│   ├── prompts.py       # 加载 prompt/*.md 和 prompt/rules/*.md，注入通用约束
│   └── utils.py         # diff 过滤、行号标注、Summary 解析、行评论校验等工具
├── prompt/
│   ├── business.md
│   ├── performance.md
│   ├── security.md
│   ├── testing.md
│   ├── documentation.md
│   ├── error_handling.md
│   ├── readability.md
│   ├── consistency.md
│   ├── maintainability.md
│   ├── dependency.md
│   ├── summary.md       # Summary Agent 的输出结构和聚合规则
│   └── rules/           # 每个维度的评分规则和 summary 分级规则
├── docs/
│   ├── context_schema.md      # context.json 字段规范
│   ├── requirements.md        # 示例 PRD
│   └── guidelines/            # 不同语言的编码规范示例
├── RUN.sh               # 审查任务启动脚本
├── INSTALL.sh           # Conda 环境和依赖安装脚本
├── config.toml.example  # 运行配置模板
├── requirements.txt     # Python 依赖：litellm、toml、pylint、pyyaml
├── context_v*.json      # 示例任务上下文
├── previous_*.json      # 示例历史审查结果
├── CR_RESULT*.md/json   # 示例或历史输出
└── cr_result/           # 示例/历史结果目录
```

## 审查任务如何启动

推荐入口是 `RUN.sh`，用法如下：

```bash
bash RUN.sh --config config.toml
```

`RUN.sh` 主要做这些事：

1. 解析 `--config` 参数，并检查配置文件是否存在。
2. 用 shell 的 `grep` / `sed` 从 TOML 中读取 `json_path`、`result_path`、`model`、`api_key`、`api_base` 等配置。
3. 将相对路径转换为绝对路径，检查 `context.json` 是否存在。
4. 从 `context.json` 中读取 `mr_iid` 和 `head_sha`，用于默认输出目录命名。
5. 如果配置了 `[context].result_path`，优先写入该目录；否则回退到 `/workspace/cr-result/<mr_iid>-<head_sha前8位>` 或 `cr-agent/results/<mr_iid>-<head_sha前8位>`。
6. 导出环境变量：
   - `CR_AGENT_CONFIG`：传给 Python 主程序读取完整 TOML。
   - `OPENAI_API_KEY` / `OPENAI_API_BASE`：传给 LiteLLM。
7. 查找名为 `cragent` 的 Conda 环境，并直接调用该环境里的 Python：

```bash
python -m agent.main
```

因此，当前真正的运行入口是 `agent/main.py`，不是 `agent/run.py`。`agent/run.py` 保留了另一套相近流程，但 `RUN.sh` 没有调用它。

## 配置和输入

配置文件参考 `config.toml.example`：

```toml
[context]
json_path = "./context.json"
result_path = "."

[project]
language = "python"
guidelines_path = ""
requirements_path = ""

[llm]
model = "gpt-4o"
api_key = "sk-xxx"
# api_base = "http://localhost:11434/v1"
# max_agent_concurrency = 10
# timeout_seconds = 240
# timeout_base_seconds = 180
# timeout_per_1k_chars = 1
# timeout_max_seconds = 600
# max_retries = 3
# retry_delay = 2.0
```

`context.json` 是一次审查任务的核心输入，字段规范见 `docs/context_schema.md`。关键字段包括：

- `task_id`：审查任务 ID。
- `project_id` / `mr_iid`：GitLab 项目和 MR 标识。
- `title` / `description`：MR 标题和描述，会进入评审上下文。
- `base_sha` / `head_sha` / `start_sha`：代码版本信息。
- `source_branch` / `target_branch`：分支信息。
- `diff_content`：本次审查的 git diff，是所有维度 Agent 的主要输入。
- `project_root`：被审查项目的工作目录，用于读取真实文件、标注行号和校验行评论。
- `requirements_Doc` 或配置里的 `requirements_path`：可选需求文档。
- `previous_report`：可选历史评审结果，用于增量复查。

## 运行流程

`agent/main.py` 的主流程如下：

1. 读取 `CR_AGENT_CONFIG` 指向的 TOML；没有环境变量时默认找项目根目录下的 `config.toml`。
2. 读取 `context.json`，提取任务信息、MR 信息、diff、项目目录、需求文档路径和历史报告。
3. 创建 `result_path`，并把 stdout/stderr 同时输出到控制台和 `<result_path>/run.log`。
4. 过滤 diff：`utils.filter_code_diff()` 只保留代码文件变更，跳过图片、压缩包、日志、锁文件等非代码内容。
5. 标注行号：如果 `project_root` 存在，`utils.annotate_diff_with_line_numbers()` 会把新增行标成类似 `0438| + ...`，方便模型输出准确行号。
6. 解析变更文件列表：`utils.parse_diff_file_paths()` 提供给一致性/linter 维度使用。
7. 初始化 `CRRouter`，传入模型、API 地址、并发数、超时和重试配置。
8. 调用 `router.route_and_aggregate()`：
   - 先并发运行 10 个维度 Agent。
   - 再运行 Summary Agent 汇总所有维度报告。
9. 解析 Summary Agent 输出：优先按 JSON 解析，兼容 YAML 和纯 Markdown 兜底。
10. 校验 `line_comments`：
    - 校验文件路径、起止行号、diff 范围和代码片段。
    - 如果行号不可靠，会生成反馈并尝试自动修正。
    - 如果需要，会临时 `git checkout <head_sha>` 到目标提交进行校验，完成后再切回原提交。
11. 写出结果文件：
    - `<result_path>/cr_result.md`：人读 Markdown 报告。
    - `<result_path>/result.json`：平台消费的结构化结果，包含 `llm_result`、`status`、`log_path`、`tokens_consume`、`line_comments`、`issues`。
    - `<result_path>/CR_REPORT.md`：兼容旧路径的 Markdown 输出。

## 评审维度

维度由 `agent/router.py` 固定注册，共 10 个：

| 维度 | Agent 类型 | 主要关注点 |
|---|---|---|
| `business` | `GenericDimensionAgent` | 是否符合 MR/PRD，业务规则、边界条件、状态流转、数据一致性 |
| `performance` | `GenericDimensionAgent` | 复杂度退化、热点阻塞、资源泄漏、SLA 风险 |
| `security` | `GenericDimensionAgent` | SQLi/XSS/命令注入、认证授权、敏感信息泄露、越权 |
| `testing` | `GenericDimensionAgent` | 关键逻辑测试、边界/异常路径、断言有效性、回归风险 |
| `documentation` | `GenericDimensionAgent` | 公共接口、关键配置、复杂逻辑说明与文档一致性 |
| `error_handling` | `GenericDimensionAgent` | 静默失败、异常吞噬、超时/回滚/恢复机制、错误可观测性 |
| `readability` | `GenericDimensionAgent` | 命名、结构、嵌套复杂度、抽象层次、阅读路径 |
| `consistency` | `QualityLinterAgent` | 项目规范、命名/风格一致性，并会对 Python/Go 尝试运行 linter |
| `maintainability` | `GenericDimensionAgent` | 重复逻辑、耦合、职责混乱、硬编码扩展点、长期修改成本 |
| `dependency` | `GenericDimensionAgent` | 依赖漏洞、版本锁定、供应链、许可证和不必要依赖 |

每个维度的角色 prompt 在 `prompt/<dimension>.md`，评分规则在 `prompt/rules/<dimension>Rule.md`。`agent/prompts.py` 会统一注入通用约束，例如：

- 只关注 diff 中新增代码行。
- 行号前缀只是元数据，不是代码。
- 输出必须符合指定结构。
- 只报告高置信度问题，避免为了凑数输出低价值建议。
- 不能把明确需求本身当成问题，只能审查实现是否偏离需求、引入副作用或缺少必要控制。

Summary Agent 使用 `prompt/summary.md` 和 `prompt/rules/summaryRule.md`，把子 Agent 上报的问题按维度阈值分成 Critical / Major / Minor，并生成最终报告、行评论和 `issues`。

Summary 分级阈值大致如下：

| 维度组 | Critical | Major | Minor | 丢弃 |
|---|---:|---:|---:|---:|
| Business / Security | `score >= 70` | `60-69` | 无 | `< 60` |
| Performance / Dependency | `score >= 90` | `80-89` | `70-79` | `< 70` |
| Maintainability / Testing / Error Handling | `score >= 95` | `80-94` | `70-79` | `< 70` |
| Consistency / Readability / Documentation | 无 | `>= 90` | `80-89` | `< 80` |

## Agent 架构分析

项目内的“Agent”主要是轻量类封装，而不是外部通用 agent 框架。

核心类：

- `BaseAgent`：封装 LiteLLM 异步调用、动态/固定超时、重试、错误分类、token 和 cost 统计。
- `GenericDimensionAgent`：单维度审查 Agent，拼装 `CODE DIFF`、语言特定检查、需求上下文和历史报告，然后调用 LLM。
- `QualityLinterAgent`：一致性维度的特殊 Agent，会先运行本地 linter，再把 linter 输出和 diff 一起交给 LLM。
- `CRRouter`：调度器。负责创建 10 个维度 Agent、控制并发、汇总 token、调用 Summary Agent。
- Summary Agent：不是独立类，而是 `BaseAgent` 加上 `SUMMARY_AGENT_PROMPT`，由 `CRRouter.generate_final_summary()` 调用。

这个架构具备明确的多 Agent 分工和并发聚合，但不包含下面这些典型 agent framework 能力：

- 没有 planner / executor / tool registry 的通用循环。
- 没有长期记忆或向量检索。
- 没有模型自主决定调用哪些工具。
- 没有多轮自我反思或自动修复代码。
- 没有任务队列、worker daemon 或服务端常驻进程。

因此可以理解为“面向代码审查场景定制的多 Prompt Agent 编排器”。

## 并发模型

### 单次审查内部并发

单次 `RUN.sh` 会启动一个新的 shell 进程，再启动一个新的 Python 进程运行 `python -m agent.main`。Python 进程内部使用 `asyncio` 并发调用 LLM。

`CRRouter.route_and_aggregate()` 会构造 10 个任务：

- `consistency`
- `business`
- `performance`
- `security`
- `testing`
- `documentation`
- `error_handling`
- `readability`
- `maintainability`
- `dependency`

如果 `max_agent_concurrency >= 10`，会直接 `asyncio.gather()` 并发执行全部 10 个维度。否则使用 `asyncio.Semaphore` 限流。例如 `max_agent_concurrency = 3` 时，同一时刻最多跑 3 个维度 Agent。

10 个维度完成后，Summary Agent 才会启动。因此 Summary 是串行的第二阶段，不和 10 个专家 Agent 同时跑。

### 多次 RUN.sh 并发

项目没有全局锁、文件锁、任务队列或去重机制。也就是说，多次同时调用 `RUN.sh` 在操作系统层面是可以并行启动的，每次都会是独立 shell 进程和独立 Python 进程。

是否安全取决于这些任务是否共享同一批路径和仓库：

1. 如果每个任务使用独立的 `context.json`、独立的 `result_path`、独立的 `project_root`，通常可以并行。
2. 如果多个任务共用同一个 `result_path`，会互相覆盖或交错写入：
   - `cr_result.md` 会被后完成的任务覆盖。
   - `result.json` 会被后完成的任务覆盖。
   - `CR_REPORT.md` 会被后完成的任务覆盖。
   - `run.log` 以 append 方式打开，多进程同时写可能交错，日志难以区分。
3. 如果多个任务共用同一个 `project_root`，存在更严重风险：
   - 行评论校验阶段可能执行 `git checkout <head_sha>`。
   - 两个进程同时 checkout 同一个工作树，会互相切换分支/提交，导致行号校验基于错误版本。
   - 一个进程恢复 original commit 时，可能把另一个进程正在使用的工作树切走。
   - 如果工作树有未提交改动，checkout 还可能失败，或让校验结果不可靠。
4. 如果多个任务共用同一个 `config.toml` 但配置不变，本身问题不大；真正的冲突点是输出目录和被审查项目目录。

结论：项目支持“进程级并行启动”，也支持“单进程内 10 维度并发”，但没有跨任务并发隔离机制。生产环境应为每个审查任务分配独立工作目录和独立结果目录，例如：

```text
workspace/<task_id>/project_code
workspace/<task_id>/context.json
workspace/<task_id>/result/
```

不建议多个任务共享同一个 `project_root` 或同一个 `result_path`。

## 异常和可靠性边界

当前实现已有的可靠性能力：

- LLM 调用支持重试，默认最多 3 次。
- 支持动态超时，也支持配置固定超时。
- 对连接错误、超时、限流、服务端 5xx、JSON 解析类问题做了分类处理。
- Summary 输出解析有多层兜底，兼容 JSON、YAML 和 Markdown。
- 行级评论会做合法性校验和自动纠偏。
- 所有 token 和 cost 会聚合进 `result.json`。
- 即使主流程异常，也会尽量写出失败状态和错误报告。

当前实现的明显边界：

- `RUN.sh` 用 `grep` / `sed` 解析 TOML，只适合简单的 `key = "value"` 格式；复杂 TOML、单引号、多行值或行内注释可能解析不准。Python 侧使用 `toml.load()`，更可靠。
- 没有全局并发锁，不能防止相同任务重复启动或多个任务写同一目录。
- 没有任务队列、取消、暂停、恢复机制。
- 没有对同一 `project_root` 的 git checkout 做互斥保护。
- `QualityLinterAgent` 只显式支持 Python 的 `pylint` 和 Go 的 `golangci-lint`；其他语言主要依赖 LLM prompt。
- 评审主要基于 diff，不读取完整项目语义；prompt 明确要求不要假设 diff 外上下文，因此复杂跨文件问题可能漏检。

## 输出文件

一次成功审查后，`RUN.sh` 会检查 `<result_path>/cr_result.md` 是否生成。主要输出如下：

```text
<result_path>/
├── run.log        # 运行日志、完整 LLM 输出、token 消耗
├── cr_result.md   # 最终 Markdown 审查报告
├── result.json    # 平台消费的结构化结果
└── CR_REPORT.md   # 兼容旧路径的 Markdown 报告
```

`result.json` 的关键字段：

- `llm_result`：最终 Markdown 报告内容。
- `status`：`success` 或 `failure`。
- `log_path`：日志路径。
- `tokens_consume`：输入 token、输出 token 和 cost。
- `line_comments`：可回写 GitLab 的行级评论。
- `issues`：Summary Agent 按规则分级后的问题列表。

## 推荐使用方式

1. 先运行安装脚本：

```bash
bash INSTALL.sh
```

2. 复制并修改配置：

```bash
cp config.toml.example config.toml
```

3. 准备 `context.json`，确保 `diff_content`、`project_root`、`head_sha`、`title`、`description` 等字段完整。

4. 启动审查：

```bash
bash RUN.sh --config config.toml
```

5. 查看结果目录中的 `cr_result.md`、`result.json` 和 `run.log`。

生产并发建议：

- 每个任务独立 clone 或 checkout 到自己的 `project_root`。
- 每个任务设置独立 `result_path`。
- 使用外部任务系统保证同一 MR/同一 commit 不被重复调度。
- 如果必须共用仓库，应在外层加文件锁，避免多个进程同时 `git checkout`。
