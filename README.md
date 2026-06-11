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