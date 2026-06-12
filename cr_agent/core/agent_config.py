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


class AgentConfig(BaseModel):
    """
    workspace/<task>/agent_config.toml 的外部运行配置契约。

    根级 platform 是推荐位置;[llm].platform 继续兼容已有样例配置。
    """

    model_config = ConfigDict(extra="allow")

    context: ContextConfig
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    llm: LlmConfig
    platform: Platform | None = None

    def configured_platform(self) -> Platform | None:
        # 根级 platform 是规范位置。[llm].platform 只是为了兼容
        # 已经把平台放在 [llm] 下的 workspace 配置。
        return self.platform or self.llm.platform


def load_agent_config(config_path: Path) -> AgentConfig:
    return AgentConfig.model_validate(toml.load(config_path))
