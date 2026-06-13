"""
工具外观层。

依赖方向:skill -> ToolFacade.tools_for(agent_name) -> ToolProvider。
skill 不感知平台,也不感知工具是否已实现。

"*" 展开语义固定为:ToolSpec 注册表全集 ∩ 当前 provider 可用集。
显式列出但 provider 不可用的工具被丢弃并记录 warning,不让 allowlist 配置
在某个平台下直接报错。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from cr_agent.core.agent_config import Platform
from cr_agent.tools.allowlist import load_agent_tools
from cr_agent.tools.catalog import CANONICAL_TOOL_NAMES
from cr_agent.tools.cr_native.registry import CrNativeToolProvider
from cr_agent.tools.infcode.adapter import InfcodeToolProvider
from cr_agent.tools.provider import ToolProvider, ToolSpec
from cr_agent.utils.logging import get_logger

_logger = get_logger("cr_agent.tools.facade")

# ToolSpec 注册表全集,用于 "*" 展开求交。
_REGISTRY_NAMES = set(CANONICAL_TOOL_NAMES)


@dataclass(frozen=True)
class ToolFacade:
    provider: ToolProvider
    agent_tools: dict[str, list[str]]

    def all_tools(self) -> list[ToolSpec]:
        return self.provider.list_tools()

    def tools_for(self, agent_name: str) -> list[ToolSpec]:
        requested = self.agent_tools.get(agent_name, [])
        available = {spec.name: spec for spec in self.provider.list_tools()}

        if "*" in requested:
            # 注册表全集 ∩ provider 可用集。
            selected = _REGISTRY_NAMES & set(available)
        else:
            selected = set()
            for tool_name in requested:
                if tool_name in available and tool_name in _REGISTRY_NAMES:
                    selected.add(tool_name)
                else:
                    _logger.warning(
                        "TOOL_ALLOWLIST_DROP agent=%s tool=%s reason=unavailable provider=%s",
                        agent_name,
                        tool_name,
                        self.provider.name(),
                    )

        # 按注册表顺序返回,保证结果稳定可预期。
        resolved = [available[name] for name in CANONICAL_TOOL_NAMES if name in selected]
        _logger.info(
            "TOOL_ALLOWLIST_LOADED agent=%s provider=%s requested=%s resolved=%s",
            agent_name,
            self.provider.name(),
            requested,
            [spec.name for spec in resolved],
        )
        return resolved


def select_tool_provider(
    platform: Platform,
    *,
    project_root: Path | None = None,
) -> ToolProvider:
    if platform == "gitlab":
        provider: ToolProvider = CrNativeToolProvider(project_root=project_root)
    elif platform == "infcode":
        provider = InfcodeToolProvider(project_root=project_root)
    else:
        raise ValueError(f"Unsupported platform for tool provider: {platform}")
    _logger.info("TOOL_PROVIDER_SELECTED platform=%s provider=%s", platform, provider.name())
    return provider


def build_tool_facade(
    provider: ToolProvider,
    agent_tools_path: Path | None = None,
) -> ToolFacade:
    return ToolFacade(provider=provider, agent_tools=load_agent_tools(agent_tools_path))


def build_tool_facade_for_platform(
    platform: Platform,
    *,
    project_root: Path | None = None,
    agent_tools_path: Path | None = None,
) -> ToolFacade:
    return build_tool_facade(
        select_tool_provider(platform, project_root=project_root),
        agent_tools_path,
    )
