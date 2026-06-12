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
