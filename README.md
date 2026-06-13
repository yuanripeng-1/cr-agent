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

## 当前项目结构

> 说明：以下为当前仓库的实际结构（重构进行中）。`skills/`、`tools/` 下已建好目录与基础文件，后续里程碑会继续补全实现。

```text
cr-agent/
├── RUN.sh
├── INSTALL.sh
├── requirements.txt
├── README.md
├── config/
│   └── router.config.json
├── docs/
│   ├── context_schema.md
│   ├── requirements.md
│   └── upgrade/
│       ├── .CR-Agent.md
│       ├── cr-agent-sdk-upgrade.md
│       └── implemented-progress.md
├── cr_agent/
│   ├── __init__.py
│   ├── main.py
│   ├── bootstrap.py
│   ├── agents/
│   │   ├── definitions.py
│   │   └── prompts/
│   │       └── .gitkeep
│   ├── core/
│   │   ├── __init__.py
│   │   └── review_input.py
│   ├── schemas/
│   │   └── summary_schema.json
│   ├── skills/
│   │   ├── registry.py
│   │   ├── collect_context/
│   │   │   ├── __init__.py
│   │   │   ├── SKILL.md
│   │   │   └── skill.py
│   │   ├── dimension_review/
│   │   │   ├── __init__.py
│   │   │   ├── SKILL.md
│   │   │   └── skill.py
│   │   ├── summarize/
│   │   │   ├── SKILL.md
│   │   │   └── skill.py
│   │   └── validate_json/
│   │       ├── SKILL.md
│   │       └── skill.py
│   ├── tools/
│   │   ├── provider.py
│   │   ├── facade.py
│   │   ├── cr_native/
│   │   │   └── registry.py
│   │   └── infcode/
│   │       └── adapter.py
│   └── utils/
│       ├── diff.py
│       ├── git_ops.py
│       └── logging.py
└── workspace/
    └── 764-ef5c5c99/
        ├── agent_config.toml
        ├── changes.diff
        └── context.json
```

## 关于 `config/` 目录

`config/` 下文件是**项目模板/静态策略配置**（后续维度与工具授权会从这里读取），
不是本次任务运行时必须传入的配置文件。
当前运行时仍然以 `workspace/<task>/agent_config.toml` 为准。

## 测试与本地 Smoke

当前已建立 pytest 测试骨架,目标是先固定可长期复用的开发模式:

```text
定义接口 → 搭建测试骨架 → Smoke Test 通过 → 开发功能 → 补充测试数据 → 持续回归
```

测试约束:

- 不访问真实外部服务。
- 不调用真实 LLM。
- 不启动 LiteLLM。
- 不调用真实 Claude Agent SDK。
- 不调用真实工具。
- runtime、主 agent、skill 均通过 fake 或 `unittest.mock.AsyncMock` 模拟依赖。

### 1. 首次准备环境

```bash
cd /Users/liyu/Desktop/MyCodeEnv/code/crAgent/cr-agent
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

### 2. 运行全部测试

```bash
cd /Users/liyu/Desktop/MyCodeEnv/code/crAgent/cr-agent
.venv/bin/python -m pytest -q
```

这条命令的含义:

- `.venv/bin/python`:使用本项目虚拟环境里的 Python。
- `-m pytest`:以 Python 模块方式运行 pytest。
- `-q`:quiet 模式,只输出简洁测试结果。

当前测试会自动发现并执行 `tests/test_*.py`,覆盖:

- `test_bootstrap_smoke.py`:启动配置与 `context.json` 装配 smoke test。
- `test_orchestrator_smoke.py`:fake runtime + fake skills 的端到端占位编排测试。
- `test_runtime_contract.py`:mock Claude SDK client 的 runtime 契约测试。
- `test_usage.py`:任务级 token usage 提取与累计测试。
- `test_artifacts.py`:`result.json`、`cr_result.md`、`run.log` 写盘测试。
- `test_skill_registry.py`:默认 skill registry 与 4 个占位 skill 链路测试。
- `test_contracts.py`:已有外部契约测试。

当前验证结果:

```text
22 passed
```

### 3. 运行当前占位审查流程

当前真实审查能力还未接入,但 `main.py → bootstrap_runtime → run_review → skill registry → artifacts`
这条占位链路已经可以跑通。

如果直接在本机使用 `workspace/15-3de6a54a` 任务目录,需要注意原始
`agent_config.toml` 使用的是容器路径:

```toml
json_path = "/workspace/15-3de6a54a/context.json"
result_path = "/workspace/cr_result/15-3de6a54a"
```

本机对应路径是:

```text
/Users/liyu/Desktop/MyCodeEnv/code/crAgent/cr-agent/workspace/15-3de6a54a
```

建议复制一份临时配置,不要直接改任务目录原文件:

```bash
cd /Users/liyu/Desktop/MyCodeEnv/code/crAgent/cr-agent

cp workspace/15-3de6a54a/agent_config.toml /private/tmp/cr-agent-15-agent_config.toml

.venv/bin/python -c "from pathlib import Path; p=Path('/private/tmp/cr-agent-15-agent_config.toml'); s=p.read_text(); s=s.replace('/workspace/15-3de6a54a','/Users/liyu/Desktop/MyCodeEnv/code/crAgent/cr-agent/workspace/15-3de6a54a'); s=s.replace('/workspace/cr_result/15-3de6a54a','/Users/liyu/Desktop/MyCodeEnv/code/crAgent/cr-agent/workspace/cr_result/15-3de6a54a'); p.write_text(s)"

.venv/bin/python -m cr_agent.main --config /private/tmp/cr-agent-15-agent_config.toml --platform gitlab
```

产物会写到:

```text
workspace/cr_result/15-3de6a54a/result.json
workspace/cr_result/15-3de6a54a/cr_result.md
workspace/cr_result/15-3de6a54a/run.log
```

说明:自 PR3 起,`main()` 是 **bootstrap-only 占位入口**:只做启动装配并写一份
`status="bootstrap_ready"` 的 `result.json` / `cr_result.md` / `run.log`,**不跑 skill、不驱动主 agent、不需 claude CLI**。
真实的 agentic 审查(主 agent 经 SDK 工具调用 skill)请用下面第 4 节的 `python -m cr_agent.smoke`。

### 4. LiteLLM agentic 冒烟(手动)

`main.py`(上面第 3 节)是 bootstrap-only 占位、不发起模型调用。要验证经
LiteLLM Anthropic-compatible Gateway 的**主 agent agentic 流程**(主 agent 经 SDK 工具
调用 collect_context / dimension_review / summarize_report,skills 仍为占位实现),使用独立入口
`cr_agent.smoke`(内部复用 `run_review`,由 `SdkMainAgentRuntime` 驱动主 agent)。

前置:

1. 已安装 `claude` CLI(见 `INSTALL.sh`)。
2. 有一个运行中的 LiteLLM Anthropic-compatible Gateway。
3. 目标 `workspace/<task>/agent_config.toml` 的 `[llm]` 已填:

```toml
[llm]
model    = "<gateway 可路由的模型名>"
api_key  = "<gateway 鉴权 key>"   # 注入为 ANTHROPIC_API_KEY
api_base = "<gateway 地址>"        # 注入为 ANTHROPIC_BASE_URL
```

运行:

```bash
cd /Users/liyu/Desktop/MyCodeEnv/code/crAgent/cr-agent
.venv/bin/python -m cr_agent.smoke --config <abs path to agent_config.toml> --platform gitlab
```

观察:

- 终端打印一次调用的 `status` 与四项 usage(input/output/cache_creation/cache_read);
- `result.json` / `cr_result.md` / `run.log` 生成;**失败时同样有产物**,且 token 为真实累计值(不写假 0);
- `run.log` 中只见 `api_key_configured=true`,**无明文 key**(统一脱敏 Filter 兜底)。

不依赖 `config/router.config.json`,CCR 不进主架构;未配置 `[llm]` 或网关不可达时,
冒烟会以失败状态结束并写出失败产物。
