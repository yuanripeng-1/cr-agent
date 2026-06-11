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

## 关于 `config/` 目录

`config/` 下文件是**项目模板/静态策略配置**（后续维度与工具授权会从这里读取），
不是本次任务运行时必须传入的配置文件。

当前运行时仍然以 `workspace/<task>/agent_config.toml` 为准。
