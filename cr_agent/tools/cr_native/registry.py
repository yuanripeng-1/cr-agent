"""
gitlab 平台的 cr-native 工具 provider。

PR1 只立边界:暴露全部平台无关工具名,但每个 handler 返回未实现占位。
真实 rg / git / ast-grep / Semble / CRG 在后续 PR(4/5/6/7)接入。
"""

from __future__ import annotations

from cr_agent.tools.catalog import build_specs
from cr_agent.tools.provider import ToolHandler, ToolResult, ToolSpec, not_implemented_result

PROVIDER_NAME = "cr-native"


def _placeholder(tool_name: str) -> ToolHandler:
    async def handler(args: dict) -> ToolResult:
        return not_implemented_result(tool_name, PROVIDER_NAME)

    return handler


class CrNativeToolProvider:
    """gitlab 平台 provider 空壳:工具齐全,实现待补。"""

    def name(self) -> str:
        return PROVIDER_NAME

    def list_tools(self) -> list[ToolSpec]:
        return build_specs(_placeholder)
