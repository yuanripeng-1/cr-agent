"""
infcode 平台的工具 provider stub。

infcode 目前只保留兼容性与扩展点:暴露相同的工具名,所有 handler 返回
NotImplemented/降级。不为它写专门 mock 行为 —— 真实能力将来委托 infcode 宿主。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from cr_agent.tools.catalog import build_specs
from cr_agent.tools.provider import ToolHandler, ToolResult, ToolSpec, not_implemented_result

PROVIDER_NAME = "infcode"


def _placeholder(tool_name: str) -> ToolHandler:
    async def handler(args: dict) -> ToolResult:
        return not_implemented_result(tool_name, PROVIDER_NAME)

    return handler


class InfcodeToolProvider:
    """infcode 平台 stub:工具名齐全,统一降级返回。"""

    def __init__(self, project_root: Path | None = None, limits: Any | None = None) -> None:
        # 签名与 CrNativeToolProvider 对齐;infcode stub 不使用这些参数。
        self._project_root = project_root

    def name(self) -> str:
        return PROVIDER_NAME

    def list_tools(self) -> list[ToolSpec]:
        return build_specs(_placeholder)

