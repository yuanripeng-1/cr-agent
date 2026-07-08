# CR-Agent 架构升级规划文档(基于 Claude Agent SDK 的多 Agent 重构)

> 状态:设计已确认,待开发。
> 配套进度文档:仓库根目录 `.CR-Agent.md`。
> 本文档是开发的唯一依据,按「里程碑 → 步骤」逐步实施。

---

## 1. 背景与目标

### 1.1 背景
- 现有 CR 项目用 `litellm` 自研了一套「扁平并发 10 维度 + 聚合 summary」的流程(`agent/router.py`、`agent/agents.py`),编排逻辑全靠自研,扩展性与可控性不足。
- 公司要把 CR 工具同时跑在两种产品形态上:
  - **gitlab**:服务端触发,使用本项目自带工具集。
  - **infcode**(类 Cursor 的 vibe coding 插件):本项目以 **skill** 形式接入 infcode,审查时由 infcode 启动本项目,**使用 infcode 宿主提供的外部 CLI 工具集**。
- 升级目标架构:**主 Agent 编排 + 4 个 Skill + 可插拔工具外观层**(见 `assets/image-98a6af5c-...png`)。

### 1.2 目标
1. 用 **Claude Agent SDK(Python `claude-agent-sdk`)** 重写编排层,获得原生 agent/tool/subagent 能力。
2. **多模型**:经 Anthropic 兼容代理层支持 claude/gpt/glm/deepseek/qwen/私有化/第三方。
3. **确定性控制**:维度数量、并发、重试等关键分支用 **Python 代码**控制(不靠 prompt),规避幻觉。
4. **工具集解耦**:启动时按平台(gitlab/infcode)用「统一工具外观层」整套切换工具集,**不使用外部 MCP 进程**。
5. **维度可配置**:gitlab 用全部维度,infcode 用部分维度(具体子集留扩展口,后续填)。
6. 启动入口仍为 `RUN.sh`;输出兼容现有 `result.json` / `cr_result.md`。

### 1.3 非目标(本期不做)
- infcode 具体维度子集、infcode CLI 工具清单与触发契约(留扩展口,产品定型后再填)。
- 评分规则/prompt 内容的重写(沿用 `prompt/` 现有 10 维度 + `summary.md` 规则,后续单独优化)。

---

## 2. 关键决策(已确认)

| 项 | 决策 |
|---|---|
| 编排框架 | Claude Agent SDK(Python),`ClaudeSDKClient` 作主 agent |
| 多模型 | Anthropic 兼容代理层;**默认 LiteLLM proxy**(主架构唯一网关),claude-code-router(CCR)**不进主架构**,仅作个人研发调试工具 |
| 编排风格 | 混合:主 agent(LLM)决策 + Skill 内部确定性 Python 编排 |
| Skill 形态 | 文件夹 = `SKILL.md`(契约/触发说明) + `skill.py`(代码);经 `@tool`+`create_sdk_mcp_server` 以**进程内工具**暴露给主 agent |
| 工具解耦 | **统一工具外观层**为唯一解耦接口;启动按平台整套切换;无外部 MCP 进程(in-process MCP 仅作 SDK 边缘适配,隐藏在外观层之下,见 §7.0) |
| 工具开放范围 | 工具集**对所有 agent 开放**(主+子均可直接调用);实际配给哪个 agent 由开发者 allowlist 决定(`config/agent_tools.toml`,见 §7.4) |
| gitlab 工具 | 本项目内置 `tools/cr_native/`,用注册表(一工具一文件,易增删) |
| infcode 工具 | **外部**,由 infcode 宿主注入;`InfcodeToolProvider` 仅做委托适配 |
| 维度配置 | `config/dimensions.toml`,`profile=platform` 决定维度列表 |
| summary | subagent 评级+总结,**默认空 allowlist(不接工具)** |
| validate | **纯确定性代码**(jsonschema),无 LLM、无工具;**纯函数只返回 `{valid, errors}`,不持有 attempt** |
| 重试 | 校验失败→主 agent 决定重新 summary(agentic);**attempt 计数与最大次数兜底归编排层(`core/orchestrator.py` + `core/state.py`)**,超限强制终止 |
| 启动与平台识别 | `RUN.sh --config <workspace/<task>/agent_config.toml> --platform <gitlab\|infcode>`;`[llm]`/根级 `platform`/`[context]` 均来自该 per-task `agent_config.toml`(仓库 `config/config.toml` 仅模板);识别优先级 `--platform` 参数 > `agent_config.toml` 根级 `platform` 字段(见 §8.1) |
| 旧代码 | `agent/` 全面废弃,目录结构重建;`prompt/` 维度 prompt 迁移复用 |

---

## 3. 总体架构

```
产物层 workspace/<task>/  ← cr_result.md · result.json · run.log · 评分明细
        ▲ 输出
主 Agent(ClaudeSDKClient,prompt 含规划,贯穿全局)
  └ 按序调用 4 个 Skill(=进程内工具):
        collect_context → dimension_review → summarize_report → validate_json
        (validate=False 时回到 summarize_report,代码计数兜底最大重试)
        ▼ skill 内部
四个 Skill(SKILL.md + skill.py 确定性编排)
  ├ collect_context : 启动 context subagent(可调工具)→ 结构化上下文
  ├ dimension_review: 代码读 dimensions.toml+platform 决定 N → asyncio.gather 并发 N 个维度 subagent(可调工具)→ 等全部 → 打分
  ├ summarize_report: summary subagent(无工具)按规则评级+总结
  └ validate_json   : 纯代码 jsonschema 校验 → {valid, errors}
        ▼ subagent 调工具
工具外观层 facade(启动时按 platform 选 provider,产出统一工具集)
  ├ CrNativeToolProvider(gitlab)→ ToolRegistry → read/glob/grep/semble/code_review_graph...
  └ InfcodeToolProvider(infcode)→ 委托 infcode 宿主外部 CLI 工具
```

**层间边界(重要)**:
- **外观层是唯一解耦接口**:所有 agent(主 + 各子 agent)都从同一个工具外观层取工具,外观层之外没有第二种接入方式。
- **工具集对所有 agent 开放**:主 agent 与所有子 agent 都「可以」直接调用工具集,不存在「工具只给子 agent」的硬限制。
- **开发者按 agent 配 allowlist**:具体每个 agent 实际能用哪些工具,由开发者在配置里(`config/agent_tools.toml`)显式指定允许清单(见 §7.4)。默认:context/dimension agent = 全量工具;summary = 空;validate 不走 agent(纯代码);主 agent = skills + 开发者按需勾选的工具。
- **Skill ≠ 工具集**:4 个 skill 是「编排能力」暴露给主 agent;工具集是「叶子能力」按 allowlist 暴露给被允许的 agent。两者都经外观层统一管理。

---

## 4. 端到端数据流(5 阶段)

1. **最初准备**:`RUN.sh` 读 `config.toml` → 定位 `context.json` → 组装 `ReviewInput`(diff + commit message + project_root + platform + 可选 previous_report/requirements)。
2. **主 agent 启动**(prompt 写好规划),依次调用 4 个 skill:
   1. `collect_context(review_input)` → 完整上下文
   2. `dimension_review(context)` → 各维度打分结果(代码确定性决定维度数与并发)
   3. `summarize_report(scores, context)` → 格式化报告(JSON 结构 + markdown)
   4. `validate_json(report)` → `{valid: bool, errors: [...]}`
      - `valid=False`:主 agent 带 `errors` 重调 `summarize_report` 再校验;代码侧 `attempt` 超 `max_retries` 强制返回终态
      - `valid=True`:输出
3. **产物层**:写 `cr_result.md` / `result.json`(兼容现字段)/ `run.log`。

---

## 5. 数据契约

### 5.1 输入 `ReviewInput`(来源:`context.json`,真实样例见 `workspace/764-ef5c5c99/context.json`)

实际由 infcode/gitlab 调用时传入,字段取自 `context.json`:

| 字段 | 来源字段 | 用途 |
|---|---|---|
| task_id | `task_id` | 任务追踪、workspace 目录 |
| project_id | `project_id` | gitlab 定位仓库 |
| mr_iid | `mr_iid` | 回写评论/查询 |
| title + description | `title`,`description` | 即 commit message / MR 说明,审查意图 |
| base_sha/head_sha/start_sha | 同名 | diff 范围、行号校验 checkout |
| source_branch/target_branch | 同名 | 分支信息 |
| diff_content | `diff_content` | 核心变更 |
| diff_file_path | `diff_file_path` | diff 文件路径 |
| project_root | `project_root` | 待审查项目目录 |
| previous_report | `previous_report` | 上轮报告(增量审查,可空) |
| requirements_doc | `requirements_Doc` | 需求文档(可空) |
| platform | `--platform` / `config.toml platform`(按 §8.1 优先级) | gitlab/infcode,决定工具集与维度 |

> 字段规范沿用 `docs/context_schema.md`;`ReviewInput` 是其强类型封装(dataclass/pydantic)。

### 5.2 维度打分 `DimensionScore`(每个维度 subagent 返回)
```
{ "dimension": "security", "score": 0-100, "confidence": 0-100,
  "findings": [ { "title","analysis","evidence","severity","file","start_line","end_line","suggestion" } ] }
```

### 5.3 最终报告 schema(`schemas/summary_schema.json`,jsonschema 校验对象)
- 决策(approve/request_changes)、总分、blockers、通过项、按维度聚合的 findings、line_comments、上下文归纳。
- 以现有 `prompt/summary.md` 产出结构 + 现 `result.json` 字段为基准定义,确保产物兼容。

---

## 6. 四个 Skill(SKILL.md + skill.py)

每个 skill 是文件夹,`skill.py` 内部编排为确定性 Python。skill 与工具同样**经外观层统一暴露**;`@tool`/`create_sdk_mcp_server` 只是 SDK 把 Python 函数交给模型调用的**进程内边缘适配**(非外部 MCP 进程,详见 §7.0),agent 层只依赖外观层、不直接耦合 MCP。

### 6.1 `collect_context`
- **SKILL.md**:告诉主 agent「准备好 ReviewInput 后调用我收集上下文」。
- **skill.py**:
  - 用 `query()`/子 client 启动 **context subagent**(系统 prompt = 上下文收集策略),挂载工具外观层工具集。
  - 子 agent 探索仓库(读相关文件、grep 引用、语义检索、CR Graph),归纳出结构化上下文。
  - 返回 `CollectedContext`(变更涉及的模块/调用链/相关文件摘要/需求映射)。

### 6.2 `dimension_review`(核心确定性控制点)
- **skill.py**:
  1. **确定性决定维度**:读 `config/dimensions.toml`,按 `platform` 取维度列表 → `N = len(list)`(代码分支,非 prompt)。
  2. 为每个维度构造维度 subagent(系统 prompt = `prompt/<dim>.md`,工具集 = 同一套外观层工具)。
  3. `asyncio.gather` + `asyncio.Semaphore(concurrency)` **并发**派发,**等待全部返回**。
  4. 收集 `List[DimensionScore]` 返回主 agent。
- **配置**:`config/dimensions.toml`
  ```toml
  [defaults]
  concurrency = 5
  [profiles.gitlab]
  dimensions = ["business","security","performance","dependency","testing",
                "error_handling","consistency","readability","maintainability","documentation"]
  [profiles.infcode]
  dimensions = []   # TODO 留扩展口:产品定型后填子集；为空时回退 defaults.fallback
  [defaults.fallback]
  dimensions = ["security","error_handling","business","readability"]
  ```

### 6.3 `summarize_report`
- **skill.py**:启动 **summary subagent(无工具)**,输入 = 所有 `DimensionScore` + 上下文 + 上轮报告 + `prompt/summary.md` 评级规则;输出格式化报告(JSON+markdown)。失败可由主 agent 重调。

### 6.4 `validate_json`
- **skill.py**:**纯函数代码**,`jsonschema.validate(report, summary_schema)`;**只返回 `{valid, errors}`**。
- **attempt 计数不在 validate_json 内**:重试次数与超限终止由**编排层**管理(`core/orchestrator.py` 的重试循环 + `core/state.py` 的 `attempt`/`max_retries`)。validate 失败时主 agent(agentic)决定是否重调 summary;`attempt` 超 `max_retries` 由编排层代码强制终止并写降级终态(status 仍 `success` + `warnings`,见 §4)。

---

## 7. 工具外观层与工具集解耦

### 7.0 设计原则:外观层是唯一接口,MCP 只是边缘适配(对应你的要求)
- **不把 MCP 当接入方式**:本项目里 agent 层与 skill 层只依赖「工具外观层」这一个抽象;它们看到的是 `ToolSpec`(平台无关的工具描述),完全不感知底层用什么 transport。
- **MCP 仅在最边缘出现**:Claude Agent SDK 给模型「自定义 Python 工具」的原生路径只有进程内 SDK MCP(`create_sdk_mcp_server`)。我们把它收敛到外观层的一个**适配器步骤**里——即「`ToolSpec` 列表 → SDK 可加载形态」的转换,**进程内、不起任何外部 MCP 进程**。换 SDK 或换 transport 只改这一个适配器,不影响工具与 agent。
- **结论**:满足「工具不要只用 MCP、统一用外观层解耦」——外观层是面向所有 agent 的统一入口;in-process MCP 是被外观层封住、可替换的实现细节;infcode 侧走 CLI 适配,完全不用 MCP。

### 7.1 抽象(transport 无关)
```python
@dataclass
class ToolSpec:                      # 平台无关的工具描述(外观层对外契约)
    name: str
    description: str
    input_schema: dict
    handler: Callable[..., Awaitable] # 实际执行(Python 函数 / CLI 包裹)

class ToolProvider(Protocol):
    def list_tools(self) -> list[ToolSpec]: ...   # 返回平台无关工具
    def name(self) -> str: ...

# 外观层:选 provider → 取 ToolSpec → 仅在边缘转成 SDK 形态
def build_tool_facade(platform, agent_tool_config) -> ToolFacade: ...
#   facade.tools_for(agent_name) -> 经 allowlist 过滤后的工具(见 7.4)
#   facade._to_sdk(specs)        -> 唯一的 MCP 边缘适配(create_sdk_mcp_server)
```
- **解耦①(外观层↔工具集)**:外观层只依赖 `ToolProvider`/`ToolSpec`,换 provider = 换整套工具。

### 7.2 gitlab:`CrNativeToolProvider` + `ToolRegistry`
- `tools/cr_native/registry.py`:装饰器 `@register_tool` 收集工具,产出 `ToolSpec`。
- 一工具一文件:`read.py`/`glob.py`/`grep.py`/`semble.py`/`code_review_graph.py`...
- **解耦②(工具集内部)**:增删工具 = 增删一个文件 + 注册,互不影响。

### 7.3 infcode:`InfcodeToolProvider`(委托外部,无 MCP)
- 不内置工具;`list_tools()` 返回的 `ToolSpec.handler` 内部用 `subprocess`/约定协议调用 **infcode 宿主提供的外部 CLI 工具**。
- 具体 CLI 清单与调用协议留扩展口(`tools/infcode/adapter.py` 定义清晰接口,先占位)。

### 7.4 工具集对所有 agent 开放 + 按 agent 配 allowlist(对应你的要求)
- **能力面**:`build_tool_facade` 产出的「整套工具集」对主 agent 与所有子 agent 都可用,不区分主/子。
- **授权面**:开发者在 `config/agent_tools.toml` 里**显式决定每个 agent 实际能用哪些工具**(allowlist),落到 SDK 的 per-agent `allowed_tools`。
  ```toml
  # 工具来源由 platform 决定(cr_native / infcode);此处只配“谁能用哪些”
  [agents.main]        # 主 agent
  skills = ["collect_context","dimension_review","summarize_report","validate_json"]
  tools  = []          # 默认不直接用叶子工具；开发者可按需加，如 ["read","grep"]

  [agents.context]     # 上下文收集 subagent
  tools  = ["*"]       # 全量工具

  [agents.dimension]   # 维度 subagent（所有维度共用同一 allowlist）
  tools  = ["*"]       # 全量工具

  [agents.summary]     # 总结 subagent
  tools  = []          # 不接工具

  # validate 不在此表：纯代码，无 agent
  ```
- `*` = 当前 platform 下的全量工具;也可写具体工具名做精细授权。新增 agent 或调整授权只改这张表,不动工具与编排代码。

---

## 8. 多模型代理层

- 主/子 agent 经 `ANTHROPIC_BASE_URL` 指向本地代理;代理把 Anthropic `/v1/messages` 转发到目标厂商。
- **默认 LiteLLM proxy**(`config/litellm.config.yaml`),作为主架构唯一 Anthropic-compatible Gateway。**CCR 不进主架构**,仅作个人研发调试工具;`config/router.config.json` 当前仅占位、主流程不加载。
- `RUN.sh` 负责:读取 **per-task `workspace/<task>/agent_config.toml`**(`--config` 指定,`[llm]`/根级 `platform`/`[context]` 均来自此文件,值由调用方按任务生成)与可选 `--platform` → 启动 Python 入口。
- 角色分级配模型:编排类(主/各 subagent)配强模型,叶子可配低成本模型。

### 8.1 平台识别机制(启动时如何区分 infcode / gitlab)

平台识别只允许两种来源,按以下优先级取第一个命中的值:

1. **`RUN.sh --platform <gitlab|infcode>`**(命令行参数,最高优先级)。
2. **per-task `agent_config.toml` 根级 `platform` 字段**。
3. 都没有 → **启动失败并报错**(不默认猜测,避免用错工具集/维度)。

> 实现备注:当前 `bootstrap.py` 在上述两源之外,还回退读取 `[llm].platform`(向后兼容超集)。如需严格执行本节两源,后续可移除该回退;在此之前以本节优先级为准、`[llm].platform` 仅作最低优先级兜底。

调用方各自要传的内容:
- **gitlab**:沿用 `RUN.sh` 调用,可直接加 `--platform gitlab`,也可在 per-task `agent_config.toml` 配 `platform = "gitlab"`。
- **infcode**:本项目作为 infcode 的 skill 被拉起时,同样使用这两种之一传入 `platform`。

`bootstrap.py` 解析出 `platform` 后,据此:① `build_tool_facade(platform)` 选工具 provider;② `dimensions.toml [profiles.<platform>]` 选维度。识别结果写入 `run.log` 首行便于排查。

---

## 9. 目录结构(全新,废弃旧 `agent/`)

```
cr-agent/
  RUN.sh  INSTALL.sh  requirements.txt  .CR-Agent.md
  config/
    config.toml                 # 模板/示例(真实运行配置来自 per-task workspace/<task>/agent_config.toml)
    dimensions.toml             # 维度 profile(按 platform)
    agent_tools.toml            # 每个 agent 的工具 allowlist(§7.4)
    litellm.config.yaml         # LiteLLM proxy 路由(主架构唯一网关)
    router.config.json          # (废弃)CCR 路由,仅个人调试,主流程不加载
  cr_agent/
    main.py                     # Python 入口
    bootstrap.py                # 平台探测 · 建外观层 · 设 env · 装配主 agent
    core/
      review_input.py           # ReviewInput(context.json 封装)
      orchestrator.py           # 装配 agentic 主 agent + 编排循环 + 重试兜底 + 汇总 usage
      sdk_runtime.py            # ClaudeAgentRuntime(ClaudeSDKClient / query 封装)
      state.py                  # attempt 计数、运行态(ReviewState)
      types.py                  # TokenUsage(input/output/cache_creation/cache_read)、QueryResult
      errors.py                 # RuntimeCallError / RuntimeTimeoutError
      usage.py                  # accumulate_usage / extract_usage(含 cache 字段)
      artifacts.py              # result.json / cr_result.md / run.log 写盘
    skills/
      registry.py               # 把 4 个 skill 注册成主 agent 工具
      collect_context/  SKILL.md  skill.py
      dimension_review/ SKILL.md  skill.py
      summarize/        SKILL.md  skill.py
      validate_json/    SKILL.md  skill.py
    agents/
      definitions.py            # 主/context/维度/summary subagent 定义
      prompts/                  # 迁移自 prompt/ 的维度 + summary prompt
    tools/
      facade.py  provider.py
      cr_native/  registry.py  read.py  glob.py  grep.py  semble.py  code_review_graph.py
      infcode/    adapter.py    # 委托外部 CLI(占位接口)
    schemas/ summary_schema.json
    utils/   diff.py(过滤/行号标注) logging.py git_ops.py(行号校验 checkout)
  docs/
    plan/2026-06-10-cr-agent-sdk-upgrade.md  # 本文
    context_schema.md  guidelines/  requirements.md
  workspace/                    # 运行产物
```

迁移映射:`agent/run.py` 的 diff 过滤/行号标注/YAML 清洗/行号校验 → `utils/`;`agent/router.py` 编排 → `core/orchestrator.py` + skills;`prompt/*.md` → `cr_agent/agents/prompts/`。

---

## 10. 实施里程碑与步骤

> 每个里程碑结束都应可独立验证;详细勾选见 `.CR-Agent.md`。

### M0 基建与脚手架
- 建新目录结构;`requirements.txt` 加 `claude-agent-sdk`、`jsonschema`、`pydantic`(或 dataclass)。
- `INSTALL.sh`:装 SDK 依赖、`claude` CLI/Node、**LiteLLM proxy**(CCR 不进主架构,仅个人调试可选装)。
- `RUN.sh`:解析 `--config/--platform`(`--config` 指向 per-task `workspace/<task>/agent_config.toml`)→ 读配置 → 起 LiteLLM 代理 → 设 env → `python -m cr_agent.main`。
- `bootstrap.py`:**平台识别(§8.1 优先级:`--platform` > `agent_config.toml` 根级 `platform`,缺失即报错)**、解析 `[llm]` 并装配 SDK env/options 骨架。
- 验证:`./RUN.sh` 能起 Python 入口;两种识别来源各验证一次,且全缺失时明确报错。

### M1 工具外观层 + cr-native 工具集
- `provider.py`(ToolProvider/ToolSpec,transport 无关)、`facade.py`(build_tool_facade + `tools_for(agent)` allowlist 过滤 + 唯一 MCP 边缘适配 `_to_sdk`)。
- `config/agent_tools.toml` + allowlist 解析(`*` 展开为全量)。
- `cr_native/registry.py` + `read/glob/grep`(先 3 个最小集),`semble`/`code_review_graph` 占位。
- 验证:platform=gitlab 下 facade 产出工具;`tools_for("summary")` 为空、`tools_for("dimension")` 为全量;单测调用 read/glob/grep。

### M2 主 agent 编排骨架(happy path)
- `sdk_runtime.py` 封装 `ClaudeSDKClient`;`orchestrator.py` 写主 agent 规划 prompt + 顺序调用 4 个 skill 的循环(skill 先用桩实现)。
- 验证:主 agent 能按序触发 4 个桩 skill 并走完流程。

### M3 collect_context
- context subagent + skill.py;产出 `CollectedContext`。
- 验证:对 `workspace/764-ef5c5c99` 真实样例能收集到相关文件/模块上下文。

### M4 dimension_review(确定性并发)
- `dimensions.toml` + 代码读 profile 决定 N;`asyncio.gather` 并发维度 subagent;迁移 `prompt/*.md`。
- 验证:platform=gitlab→10 维度并发;改 infcode→走 fallback 子集;打印实际并发数。

### M5 summarize
- summary subagent(无工具)+ `prompt/summary.md` 评级规则;产出报告 JSON+markdown。
- 验证:对 M4 打分能产出结构化报告。

### M6 validate_json + 重试环
- `summary_schema.json`;`validate_json` 纯代码校验;orchestrator 接重试兜底(max_retries)。
- 验证:构造非法报告→valid=false→主 agent 重 summary;超限强制终止。

### M7 产物层(兼容输出)
- 迁移行号校验(git checkout/restore)、`result.json`/`cr_result.md` 渲染。
- 验证:产物字段与现有消费方兼容;行号校验通过。

### M8 infcode provider + skill 封装
- `InfcodeToolProvider` 委托外部 CLI(按占位接口);本项目作为 infcode skill 的接入封装。
- 验证:platform=infcode 时 facade 切到 infcode 适配器(可用 mock CLI 验证切换)。

### M9 加固
- 重试/超时/限流、结构化日志与 telemetry、关键路径单测、README/文档更新。

---

## 11. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 弱模型 tool-use 质量差 | 编排角色配强模型;代理开 tool 增强;jsonschema+重试兜底 |
| SDK 运行依赖(claude CLI/Node) | INSTALL.sh 固化;gitlab CI 与 infcode 环境预装校验 |
| 成本/时延(多 subagent) | 并发上限、角色分级模型、上下文复用 |
| infcode 接入契约未知 | `InfcodeToolProvider` 定义干净适配接口,先占位,产品定型再填 |
| 行号回写错位 | 沿用现有 checkout 校验逻辑迁移到 `utils/git_ops.py` |

---

## 12. 待产品确定后回填的扩展口(TODO)
- `config/dimensions.toml [profiles.infcode]` 维度子集。
- `tools/infcode/adapter.py` 的 infcode CLI 工具清单与调用协议。
- infcode 触发本项目的具体契约(入参传递方式)。
