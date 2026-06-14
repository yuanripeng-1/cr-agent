"""
cr-native 外部工具 adapter(PR6)。

本模块集中封装外部命令名与调用方式,skill 只感知平台无关工具名。
所有外部工具缺失、失败、超时都返回统一降级结构 {ok, data, warnings, error},
不抛穿主审查流程。
"""

from __future__ import annotations

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from cr_agent.tools.cr_native.fs_tools import ToolLimits, _guard, _require_root, safe_resolve
from cr_agent.tools.provider import ToolHandler, ToolResult, error_result, ok_result
from cr_agent.utils.logging import get_logger

if TYPE_CHECKING:
    from cr_agent.tools.cr_native.crg_lifecycle import CrgLifecycle

_logger = get_logger("cr_agent.tools.cr_native.external")


@dataclass(frozen=True)
class ExternalTool:
    name: str
    candidates: tuple[str, ...]
    required: bool = False


EXTERNAL_TOOLS: tuple[ExternalTool, ...] = (
    ExternalTool("git", ("git",), required=True),
    ExternalTool("rg", ("rg",), required=True),
    ExternalTool("ast-grep", ("ast-grep", "sg")),
    ExternalTool("semble", ("semble",)),
    ExternalTool("code-review-graph", ("code-review-graph", "crg")),
)


def resolve_command(candidates: tuple[str, ...]) -> str | None:
    for command in candidates:
        if shutil.which(command):
            return command
    return None


def check_external_tool_availability() -> dict[str, dict[str, object]]:
    """运行时外部工具可用性检查。缺失只记录 warning,不阻断启动。"""
    status: dict[str, dict[str, object]] = {}
    for tool in EXTERNAL_TOOLS:
        command = resolve_command(tool.candidates)
        available = command is not None
        status[tool.name] = {
            "available": available,
            "command": command,
            "candidates": list(tool.candidates),
            "required": tool.required,
        }
        if available:
            _logger.info(
                "EXTERNAL_TOOL_AVAILABLE tool=%s command=%s",
                tool.name,
                command,
            )
        else:
            _logger.warning(
                "EXTERNAL_TOOL_MISSING tool=%s candidates=%s required=%s",
                tool.name,
                ",".join(tool.candidates),
                tool.required,
            )
    return status


def _missing_result(tool_name: str, candidates: tuple[str, ...]) -> ToolResult:
    warning = f"{tool_name} unavailable: install one of {', '.join(candidates)}"
    return error_result(warning, warnings=[warning])


async def _run_external(
    command: list[str],
    *,
    cwd: Path,
    timeout_s: float,
    max_bytes: int,
    tool_name: str,
) -> ToolResult:
    try:
        proc = await asyncio.create_subprocess_exec(
            *command,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        return _missing_result(tool_name, (command[0],))

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        warning = f"{tool_name} timed out after {timeout_s}s"
        return error_result(warning, warnings=[warning])

    warnings: list[str] = []
    if len(stdout) > max_bytes:
        stdout = stdout[:max_bytes]
        warnings.append(f"{tool_name} output truncated to {max_bytes} bytes")

    text = stdout.decode("utf-8", errors="replace")
    err = stderr.decode("utf-8", errors="replace").strip()
    if proc.returncode != 0:
        warning = f"{tool_name} failed: {err or f'exit {proc.returncode}'}"
        return error_result(warning, warnings=[warning])

    return ok_result({"stdout": text, "lines": [line for line in text.splitlines() if line]}, warnings)


def make_ast_grep_search(project_root: Path | None, limits: ToolLimits) -> ToolHandler:
    async def handler(args: dict) -> ToolResult:
        guard = _require_root(project_root, "ast_grep_search")
        if guard is not None:
            return guard

        async def work() -> ToolResult:
            candidates = ("ast-grep", "sg")
            command = resolve_command(candidates)
            if command is None:
                return _missing_result("ast_grep_search", candidates)

            pattern = str(args.get("pattern") or "")
            if not pattern:
                return error_result("ast_grep_search requires a pattern")

            cmd = [command, "run", "--pattern", pattern]
            lang = args.get("lang")
            if lang:
                cmd += ["--lang", str(lang)]
            search_path = args.get("path")
            if search_path:
                safe_resolve(project_root, str(search_path))
                cmd.append(str(search_path))
            else:
                cmd.append(".")
            result = await _run_external(
                cmd,
                cwd=project_root.resolve(),
                timeout_s=limits.timeout_s,
                max_bytes=limits.max_grep_bytes,
                tool_name="ast_grep_search",
            )
            if result["ok"]:
                result["data"] = {
                    "pattern": pattern,
                    "matches": result["data"]["lines"],
                }
            return result

        return await _guard(work(), tool_name="ast_grep_search", timeout_s=limits.timeout_s)

    return handler


def make_semble_search(project_root: Path | None, limits: ToolLimits) -> ToolHandler:
    async def handler(args: dict) -> ToolResult:
        guard = _require_root(project_root, "semble_search")
        if guard is not None:
            return guard

        async def work() -> ToolResult:
            candidates = ("semble",)
            command = resolve_command(candidates)
            if command is None:
                return _missing_result("semble_search", candidates)

            query = str(args.get("query") or "")
            if not query:
                return error_result("semble_search requires a query")

            cmd = [command, "search", query]
            top_k = args.get("top_k")
            if top_k is not None:
                cmd += ["--top-k", str(int(top_k))]
            result = await _run_external(
                cmd,
                cwd=project_root.resolve(),
                timeout_s=limits.timeout_s,
                max_bytes=limits.max_grep_bytes,
                tool_name="semble_search",
            )
            if result["ok"]:
                result["data"] = {"query": query, "matches": result["data"]["lines"]}
            return result

        return await _guard(work(), tool_name="semble_search", timeout_s=limits.timeout_s)

    return handler


def make_crg_status(
    project_root: Path | None, limits: ToolLimits, crg_lifecycle: "CrgLifecycle | None" = None
) -> ToolHandler:
    async def handler(args: dict) -> ToolResult:
        if crg_lifecycle is not None:
            return crg_lifecycle.status()

        guard = _require_root(project_root, "crg_status")
        if guard is not None:
            return guard

        async def work() -> ToolResult:
            candidates = ("code-review-graph", "crg")
            command = resolve_command(candidates)
            if command is None:
                return _missing_result("crg_status", candidates)
            return await _run_external(
                [command, "status"],
                cwd=project_root.resolve(),
                timeout_s=limits.timeout_s,
                max_bytes=limits.max_grep_bytes,
                tool_name="crg_status",
            )

        return await _guard(work(), tool_name="crg_status", timeout_s=limits.timeout_s)

    return handler


def make_crg_query(
    project_root: Path | None, limits: ToolLimits, crg_lifecycle: "CrgLifecycle | None" = None
) -> ToolHandler:
    async def handler(args: dict) -> ToolResult:
        query = str(args.get("query") or "")
        if not query:
            return error_result("crg_query requires a query")
        if crg_lifecycle is not None:
            return await crg_lifecycle.query(query)

        guard = _require_root(project_root, "crg_query")
        if guard is not None:
            return guard

        async def work() -> ToolResult:
            candidates = ("code-review-graph", "crg")
            command = resolve_command(candidates)
            if command is None:
                return _missing_result("crg_query", candidates)

            result = await _run_external(
                [command, "query", query],
                cwd=project_root.resolve(),
                timeout_s=limits.timeout_s,
                max_bytes=limits.max_grep_bytes,
                tool_name="crg_query",
            )
            if result["ok"]:
                result["data"] = {"query": query, "results": result["data"]["lines"]}
            return result

        return await _guard(work(), tool_name="crg_query", timeout_s=limits.timeout_s)

    return handler
