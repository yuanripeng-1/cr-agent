# 项目工作说明

## 项目概述

CR-Agent 是基于 **Python 3.11 + Claude Agent SDK + LiteLLM 网关** 的 MR/PR 自动化代码审查引擎。读取 `workspace/<task>/context.json` 与 `agent_config.toml`，经主 Agent 编排四阶段 Skill 流水线，产出 `result.json` / `cr_result.md` / `run.log`。

**架构约束**：控制流反转——主 Agent 通过 MCP skill 工具决定阶段顺序；`orchestrator` 只做兜底重试、usage 汇总、产物写盘。新增逻辑必须落在对应分层，禁止擅自引入新框架或改写整体分层。

## 常用命令

```bash
# 安装（conda + 依赖 + 外部工具检查）
bash INSTALL.sh

# 完整审查（生产入口）
bash RUN.sh --config workspace/<task>/agent_config.toml --platform gitlab

# 仅验证 bootstrap 装配
bash RUN.sh --config <agent_config.toml> --platform gitlab --bootstrap-only

# 全量单测（必须 mock，不连真实 LLM/SDK）
conda run -n cragent python -m pytest -q

# 单文件/单测
conda run -n cragent python -m pytest -q tests/test_collect_context.py

# Agentic 冒烟（需 claude CLI + LiteLLM 网关 + [llm] 配置）
conda run -n cragent python -m cr_agent.smoke --config <abs_path>/agent_config.toml --platform gitlab

# 工具层冒烟（需真实 project_root + rg）
conda run -n cragent python -m cr_agent.tools_smoke --config <abs_path>/agent_config.toml --platform gitlab

# 契约校验（config / context / result 三件套）
conda run -n cragent python -m cr_agent.core.verify_contracts --config <cfg> --context <ctx> --result <result>
```

无 conda 时可用 `.venv/bin/python` 替代，但项目标准环境是 `cragent`。

## 目录结构

| 路径 | 职责 | 改动指引 |
|------|------|----------|
| `cr_agent/main.py` | CLI 入口 | 只接参数、调 bootstrap + run_review，不写业务 |
| `cr_agent/bootstrap.py` | 装配 `RuntimeContext` | 配置加载、ToolFacade、3× subagent runtime |
| `cr_agent/core/` | 编排、主 Agent、契约模型、产物 | 改流水线/重试/写盘逻辑来这里 |
| `cr_agent/skills/` | 四阶段审查逻辑 | 新业务阶段放 `skills/<name>/skill.py` + `SKILL.md`，并在 `registry.py` 注册 |
| `cr_agent/tools/` | 平台无关工具契约 + Provider | 新工具先改 `catalog.py`，再实现 provider handler |
| `cr_agent/agents/` | Agent 定义与 prompt 占位 | 新 subagent 契约与 prompt |
| `config/` | 静态策略模板（维度、工具 allowlist） | 非单次任务运行时配置 |
| `prompt/` | 维度审查与汇总 prompt 文本 | 改审查话术来这里，不改 skill 控制流 |
| `workspace/` | 任务输入与审查产物 | **运行时配置**在 `<task>/agent_config.toml`；勿擅自改用户任务目录 |
| `tests/` | 回归测试 | 新功能必须补对应 `test_*.py` |

## 编码规范

1. **分层不可穿透**：Skill 通过 `runtime_context.tool_facade.tools_for(agent_name)` 调工具，禁止直连 provider 或 subprocess；编排/重试逻辑放 `core/orchestrator.py`，禁止在 `main.py` 堆业务。
2. **扩展走注册表**：新 Skill → `skills/registry.py`；新 Tool → `tools/catalog.py` + provider + `config/agent_tools.toml` allowlist；新 Agent → `bootstrap.py` 注入 runtime + `agent_tools.toml`。
3. **契约优先**：输入/输出用 `core/` 下 Pydantic 模型校验；`RuntimeContext` 是 `frozen` dataclass，可变状态放 `ReviewState` / `MainAgentSession`，禁止原地修改 context。
4. **工具返回统一**：所有工具 handler 返回 `{ok, data, warnings, error}`；失败降级不抛穿流水线；日志禁止输出 `api_key` / `git_token` 明文。

## 禁止事项

- 严禁提交密钥、`.env`、`agent_config.toml` 中的 `[llm].api_key` 等敏感信息。
- 严禁擅自修改 JSON/Pydantic 契约字段（`context.json`、`result.json`、`ReviewResult` 等）而不同步更新测试与 `verify_contracts`。
- 严禁随意升级 `claude-agent-sdk` 等核心依赖版本（改 `requirements.txt` 需明确理由）。
- 未经允许不得删除或覆盖 `workspace/` 下用户已有任务配置、diff、审查产物。
- 未经允许不得执行 `git_checkout` 或隐式切分支；工作区应已处于 `source_branch`。

## 验证要求

改动 Python 代码后**必须**运行：

```bash
conda run -n cragent python -m pytest -q
```

- 测试约束：不访问真实外部服务、不调用真实 LLM、不启动 LiteLLM、不调真实 Claude SDK（用 `tests/fakes.py` 或 `unittest.mock.AsyncMock`）。
- 若改动涉及契约模型，额外跑 `verify_contracts` 或对应 `tests/test_contracts.py`。
- 项目未配置 ruff/black/mypy，不以格式化工具作为 gate。
- 若改动 smoke 路径且本地无 LiteLLM/claude CLI，说明无法跑 agentic 冒烟，但以 pytest 全绿为最低门槛。

## 常见坑

1. **混淆入口**：`main.py` / `RUN.sh` 是完整审查；`--bootstrap-only` 只验装配；真实 SDK 联调用 `cr_agent.smoke`，工具联调用 `cr_agent.tools_smoke`——三者不可互换。
2. **路径陷阱**：任务目录 `agent_config.toml` 常含容器路径 `/workspace/...`；本机运行需复制到临时文件并替换为绝对路径，禁止直接改原任务文件。
3. **测试偷连真网**：`dimension_review` / `collect_context` 等 async skill 测试必须 mock `query_subagent`；新增测试若 import 了真实 SDK client，CI 会失败或变慢。
