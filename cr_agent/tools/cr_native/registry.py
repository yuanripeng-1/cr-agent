"""
gitlab 平台的 cr-native 工具 provider。

已做实:文件工具(read_file / read_file_range / glob_files / grep_text)、最小只读
git 工具(git_status / git_rev_parse)、受控 git 接口(git_fetch / git_checkout),以及
外部工具 adapter 骨架(ast_grep_search / semble_search / crg_status / crg_query)。
"""

from __future__ import annotations

from pathlib import Path

from cr_agent.tools.catalog import build_specs
from cr_agent.tools.cr_native.crg_lifecycle import CrgLifecycle
from cr_agent.tools.cr_native.fs_tools import (
    ToolLimits,
    make_glob_files,
    make_grep_text,
    make_read_file,
    make_read_file_range,
)
from cr_agent.tools.cr_native.external_tools import (
    make_ast_grep_search,
    make_crg_affected_flows,
    make_crg_callees,
    make_crg_callers,
    make_crg_get_flow,
    make_crg_query,
    make_crg_status,
    make_semble_search,
)
from cr_agent.tools.cr_native.git_tools import (
    GitSettings,
    make_git_checkout,
    make_git_fetch,
    make_git_rev_parse,
    make_git_status,
)
from cr_agent.tools.provider import ToolHandler, ToolResult, ToolSpec, not_implemented_result

PROVIDER_NAME = "cr-native"


class CrNativeToolProvider:
    """gitlab 平台 provider:本地文件/只读 git 工具已实现,外部工具待补。"""

    def __init__(
        self,
        project_root: Path | None = None,
        limits: ToolLimits | None = None,
        git_settings: GitSettings | None = None,
        crg_lifecycle: CrgLifecycle | None = None,
    ) -> None:
        self._project_root = project_root
        self._limits = limits or ToolLimits()
        self._git = git_settings or GitSettings()
        self._crg = crg_lifecycle

    def name(self) -> str:
        return PROVIDER_NAME

    def _handler_factory(self, tool_name: str) -> ToolHandler:
        root, limits, git, crg = self._project_root, self._limits, self._git, self._crg
        if tool_name == "read_file":
            return make_read_file(root, limits)
        if tool_name == "read_file_range":
            return make_read_file_range(root, limits)
        if tool_name == "glob_files":
            return make_glob_files(root, limits)
        if tool_name == "grep_text":
            return make_grep_text(root, limits)
        if tool_name == "git_status":
            return make_git_status(root, git)
        if tool_name == "git_rev_parse":
            return make_git_rev_parse(root, git)
        if tool_name == "git_fetch":
            return make_git_fetch(root, git)
        if tool_name == "git_checkout":
            return make_git_checkout(root, git)
        if tool_name == "ast_grep_search":
            return make_ast_grep_search(root, limits)
        if tool_name == "semble_search":
            return make_semble_search(root, limits)
        if tool_name == "crg_status":
            return make_crg_status(root, limits, crg)
        if tool_name == "crg_query":
            return make_crg_query(root, limits, crg)
        if tool_name == "crg_callers":
            return make_crg_callers(root, limits, crg)
        if tool_name == "crg_callees":
            return make_crg_callees(root, limits, crg)
        if tool_name == "crg_affected_flows":
            return make_crg_affected_flows(root, limits, crg)
        if tool_name == "crg_get_flow":
            return make_crg_get_flow(root, limits, crg)

        async def placeholder(args: dict) -> ToolResult:
            return not_implemented_result(tool_name, PROVIDER_NAME)

        return placeholder

    def list_tools(self) -> list[ToolSpec]:
        return build_specs(self._handler_factory)
