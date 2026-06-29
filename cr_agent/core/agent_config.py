from __future__ import annotations

from pathlib import Path
from typing import Literal, get_args

import toml
from pydantic import BaseModel, ConfigDict, Field


Platform = Literal["gitlab", "infcode"]
VALID_PLATFORMS = set(get_args(Platform))


class ContextConfig(BaseModel):
    # 运行路径字段必须严格校验。这里如果拼错,通常会导致 agent 读取错误任务
    # 或把结果写到非预期目录。
    model_config = ConfigDict(extra="forbid")

    json_path: str = Field(min_length=1)
    result_path: str = ""


class ProjectConfig(BaseModel):
    # 项目元信息允许随产品演进扩展。当前流水线只依赖少数字段,
    # 所以未知字段保留而不是拒绝。
    model_config = ConfigDict(extra="allow")

    language: str = ""
    status: str = ""
    guidelines_path: str = ""
    requirements_path: str = ""


class LlmConfig(BaseModel):
    # LLM 配置保持可扩展,因为不同 Anthropic 兼容代理可能需要
    # 各自的 provider 专属参数。
    model_config = ConfigDict(extra="allow")

    model: str = Field(min_length=1)
    api_key: str = ""
    api_base: str = ""
    platform: Platform | None = None
    # 是否在本机起 LiteLLM proxy 做 Anthropic<->OpenAI 协议转换。
    # 默认关:沿用直连(仅适用于原生支持 Anthropic /v1/messages 的网关)。
    # 开启后,bootstrap 会把 api_base/api_key 改写为本地 proxy。
    use_litellm_gateway: bool = False
    # LiteLLM 路由前缀(上游协议)。OpenAI 兼容厂商用 "openai"。
    gateway_provider: str = "openai"
    # 本地 proxy 端口;留空自动取空闲端口。
    gateway_port: int | None = None
    # 隔离的 claude CLI 配置目录;由 bootstrap 在启动网关后注入,
    # 经 build_sdk_env 落到 CLAUDE_CONFIG_DIR,使 CLI 不读宿主 ~/.claude/settings.json。
    claude_config_dir: str = ""
    # summary agent 是否走 LiteLLM structured output（response_format json_schema）。
    # 默认开启；对不支持 json_schema 的 OpenAI 兼容模型可设为 false，改走 SDK。
    summary_structured_output: bool = True


class GitConfig(BaseModel):
    # git 配置全部可选,缺省时只读本地 git 仍可用、远程默认关闭。
    model_config = ConfigDict(extra="allow")

    # 全局兜底 token;per-task context.git_token 优先级更高。日志必须脱敏。
    token: str = ""
    # git 命令超时(秒)。
    timeout_s: float = 30.0
    # 是否允许联网(git_fetch 等);默认关闭,避免引入不可控远程成本。
    allow_network: bool = False


class SembleConfig(BaseModel):
    # semble_search 冷启动(加载 Model2Vec/建索引)较慢,默认 120s。
    model_config = ConfigDict(extra="allow")

    timeout_s: float = 120.0


class CrgConfig(BaseModel):
    # CRG 默认关闭,避免首次接入时引入后台构建成本。
    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    base_dir: str = ".crg"
    # 兼容旧配置。code-review-graph 2.x 真实 CLI 基于 project_root --repo 构建,
    # 不再读取 target_root。
    target_root: str = ""
    max_retry: int = 3
    retry_interval_s: float = 1.0
    timeout_s: float = 60.0
    # 调用链工具(crg_callers/callees/affected_flows/get_flow)所用的 crg 环境 python。
    # 留空则自动从 code-review-graph 命令位置推断;也可用环境变量 CRG_PYTHON 覆盖。
    python_path: str = ""


class ToolsConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    crg: CrgConfig = Field(default_factory=CrgConfig)
    semble: SembleConfig = Field(default_factory=SembleConfig)


class TimeoutsConfig(BaseModel):
    # 各子 agent 的单次模型调用超时(秒)。summary 输入/输出最大,默认与其它一致 300s。
    model_config = ConfigDict(extra="allow")

    context_s: float = 300.0
    dimension_s: float = 300.0
    summary_s: float = 300.0


class AgentConfig(BaseModel):
    """
    workspace/<task>/agent_config.toml 的外部运行配置契约。

    根级 platform 是推荐位置;[llm].platform 继续兼容已有样例配置。
    """

    model_config = ConfigDict(extra="allow")

    context: ContextConfig
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    llm: LlmConfig
    git: GitConfig = Field(default_factory=GitConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    timeouts: TimeoutsConfig = Field(default_factory=TimeoutsConfig)
    platform: Platform | None = None

    def configured_platform(self) -> Platform | None:
        # 根级 platform 是规范位置。[llm].platform 只是为了兼容
        # 已经把平台放在 [llm] 下的 workspace 配置。
        return self.platform or self.llm.platform


def load_agent_config(config_path: Path) -> AgentConfig:
    return AgentConfig.model_validate(toml.load(config_path))
