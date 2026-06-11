from __future__ import annotations

from dataclasses import dataclass

from cr_agent.tools.provider import ToolProvider, ToolSpec


@dataclass
class ToolFacade:
    provider: ToolProvider

    def all_tools(self) -> list[ToolSpec]:
        return self.provider.list_tools()


def build_tool_facade(provider: ToolProvider) -> ToolFacade:
    return ToolFacade(provider=provider)

