"""
gitlab 平台的 cr-native 工具 provider。

PR4:read_file / read_file_range / glob_files / grep_text 已做实(限制在 project_root 下、
超时/截断/结构化返回);其余工具(ast_grep_search / git_* / semble_* / crg_*)仍返回未实现占位。
"""

from __future__ import annotations

from pathlib import Path

from cr_agent.tools.catalog import build_specs
from cr_agent.tools.cr_native.fs_tools import (
    ToolLimits,
    make_glob_files,
    make_grep_text,
    make_read_file,
    make_read_file_range,
)
from cr_agent.tools.provider import ToolHandler, ToolResult, ToolSpec, not_implemented_result

PROVIDER_NAME = "cr-native"


class CrNativeToolProvider:
    """gitlab 平台 provider:本地文件工具已实现,外部工具待补。"""

    def __init__(
        self,
        project_root: Path | None = None,
        limits: ToolLimits | None = None,
    ) -> None:
        self._project_root = project_root
        self._limits = limits or ToolLimits()

    def name(self) -> str:
        return PROVIDER_NAME

    def _handler_factory(self, tool_name: str) -> ToolHandler:
        root, limits = self._project_root, self._limits
        if tool_name == "read_file":
            return make_read_file(root, limits)
        if tool_name == "read_file_range":
            return make_read_file_range(root, limits)
        if tool_name == "glob_files":
            return make_glob_files(root, limits)
        if tool_name == "grep_text":
            return make_grep_text(root, limits)

        async def placeholder(args: dict) -> ToolResult:
            return not_implemented_result(tool_name, PROVIDER_NAME)

        return placeholder

    def list_tools(self) -> list[ToolSpec]:
        return build_specs(self._handler_factory)
