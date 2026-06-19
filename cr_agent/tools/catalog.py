"""
平台无关的工具目录(canonical ToolSpec 注册表)。

这里定义全集:工具名 + JSON Schema + 描述。CrNativeToolProvider 与
InfcodeToolProvider 都从同一份目录构建各自的 ToolSpec,只替换 handler。
这样 "*" 展开的语义 “注册表全集 ∩ provider 可用集” 在两个平台下都可预期。

PR1 不实现任何真实工具,handler 由各 provider 注入占位实现。
"""

from __future__ import annotations

from typing import Any, Callable

from cr_agent.tools.provider import JsonSchema, ToolHandler, ToolSpec


def _obj(properties: dict[str, Any], required: list[str]) -> JsonSchema:
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


_STR = {"type": "string"}
_INT = {"type": "integer"}


# 工具名保持平台无关。顺序即 "*" 展开后的稳定返回顺序。
CANONICAL_TOOLS: dict[str, tuple[str, JsonSchema]] = {
    "read_file": (
        "Read a UTF-8 text file under project_root.",
        _obj({"path": _STR}, ["path"]),
    ),
    "read_file_range": (
        "Read a line range of a text file under project_root.",
        _obj({"path": _STR, "start_line": _INT, "end_line": _INT}, ["path", "start_line", "end_line"]),
    ),
    "glob_files": (
        "List files matching a glob pattern under project_root.",
        _obj({"pattern": _STR, "root": _STR}, ["pattern"]),
    ),
    "grep_text": (
        "Search file contents for a regex pattern.",
        _obj({"pattern": _STR, "path": _STR, "glob": _STR}, ["pattern"]),
    ),
    "ast_grep_search": (
        "Structural code search using ast-grep.",
        _obj({"pattern": _STR, "lang": _STR, "path": _STR}, ["pattern"]),
    ),
    "git_status": (
        "Show working tree status (read-only).",
        _obj({}, []),
    ),
    "git_rev_parse": (
        "Resolve a ref to a commit SHA (read-only).",
        _obj({"ref": _STR}, []),
    ),
    "git_fetch": (
        "Fetch refs from a remote.",
        _obj({"remote": _STR, "ref": _STR}, []),
    ),
    "git_checkout": (
        "Checkout a ref in the working tree.",
        _obj({"ref": _STR}, ["ref"]),
    ),
    "semble_search": (
        "Semantic code search via Semble.",
        _obj({"query": _STR, "top_k": _INT}, ["query"]),
    ),
    "crg_status": (
        "Report code-review-graph build status.",
        _obj({}, []),
    ),
    "crg_build_or_update": (
        "Trigger a code-review-graph build or update.",
        _obj({}, []),
    ),
    "crg_query": (
        "Query the code-review-graph.",
        _obj({"query": _STR}, ["query"]),
    ),
    "crg_callers": (
        "List direct callers (one hop) of a symbol via the code-review-graph. "
        "target is a symbol name or 'path::funcName'.",
        _obj({"target": _STR, "limit": _INT}, ["target"]),
    ),
    "crg_callees": (
        "List direct callees (one hop) of a symbol via the code-review-graph. "
        "target is a symbol name or 'path::funcName'.",
        _obj({"target": _STR, "limit": _INT}, ["target"]),
    ),
    "crg_affected_flows": (
        "List execution flows affected by the MR changes via the code-review-graph.",
        _obj({"base": _STR, "limit": _INT}, []),
    ),
    "crg_get_flow": (
        "Get one execution flow's step path via the code-review-graph. "
        "Provide flow_name or flow_id.",
        _obj({"flow_name": _STR, "flow_id": _INT, "limit": _INT}, []),
    ),
}

# ToolSpec 注册表全集的工具名集合,"*" 展开以它为基准与 provider 可用集求交。
CANONICAL_TOOL_NAMES: tuple[str, ...] = tuple(CANONICAL_TOOLS.keys())


def build_specs(handler_factory: Callable[[str], ToolHandler]) -> list[ToolSpec]:
    """
    用 handler_factory(tool_name) 为目录里每个工具生成 handler,产出该 provider 的
    ToolSpec 列表。provider 只需关心 handler,schema/description 来自目录。
    """
    specs: list[ToolSpec] = []
    for tool_name, (description, schema) in CANONICAL_TOOLS.items():
        specs.append(
            ToolSpec(
                name=tool_name,
                description=description,
                input_schema=schema,
                handler=handler_factory(tool_name),
            )
        )
    return specs
