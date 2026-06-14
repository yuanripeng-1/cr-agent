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


class GitConfig(BaseModel):
    # git 配置全部可选,缺省时只读本地 git 仍可用、远程默认关闭。
    model_config = ConfigDict(extra="allow")

    # 全局兜底 token;per-task context.git_token 优先级更高。日志必须脱敏。
    token: str = ""
    # git 命令超时(秒)。
    timeout_s: float = 30.0
    # 是否允许联网(git_fetch 等);默认关闭,避免引入不可控远程成本。
    allow_network: bool = False


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


class ToolsConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    crg: CrgConfig = Field(default_factory=CrgConfig)


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
    platform: Platform | None = None

    def configured_platform(self) -> Platform | None:
        # 根级 platform 是规范位置。[llm].platform 只是为了兼容
        # 已经把平台放在 [llm] 下的 workspace 配置。
        return self.platform or self.llm.platform


def load_agent_config(config_path: Path) -> AgentConfig:
    return AgentConfig.model_validate(toml.load(config_path))
