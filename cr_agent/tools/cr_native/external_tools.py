"""
cr-native 外部工具 adapter。

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
    ExternalTool("ast-grep", ("ast-grep",)),
    ExternalTool("semble", ("semble",)),
    ExternalTool("code-review-graph", ("code-review-graph",)),
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
            candidates = ("ast-grep",)
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
                # 用 safe_resolve 的返回值(已规范化、确认在 root 内)构建相对路径,
                # 而非原始用户输入,避免路径遍历防护被绕过。
                resolved = safe_resolve(project_root, str(search_path))
                cmd.append(str(resolved.relative_to(project_root.resolve())))
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
                timeout_s=limits.semble_timeout_s,
                max_bytes=limits.max_grep_bytes,
                tool_name="semble_search",
            )
            if result["ok"]:
                result["data"] = {"query": query, "matches": result["data"]["lines"]}
            return result

        return await _guard(work(), tool_name="semble_search", timeout_s=limits.semble_timeout_s)

    return handler


def make_crg_build_or_update(
    project_root: Path | None, limits: ToolLimits, crg_lifecycle: "CrgLifecycle | None" = None
) -> ToolHandler:
    async def handler(args: dict) -> ToolResult:
        if crg_lifecycle is None:
            return _crg_graph_unavailable("crg_build_or_update")
        return await crg_lifecycle.build_or_update()

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
            candidates = ("code-review-graph",)
            command = resolve_command(candidates)
            if command is None:
                return _missing_result("crg_status", candidates)
            return await _run_external(
                [command, "status", "--repo", str(project_root.resolve())],
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
            candidates = ("code-review-graph",)
            command = resolve_command(candidates)
            if command is None:
                return _missing_result("crg_query", candidates)

            result = await _run_external(
                [command, "detect-changes", "--repo", str(project_root.resolve()), "--base", query],
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


_DEFAULT_GRAPH_LIMIT = 20


def _coerce_limit(raw: object) -> int:
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return _DEFAULT_GRAPH_LIMIT
    return value if value > 0 else _DEFAULT_GRAPH_LIMIT


def _crg_graph_unavailable(tool_name: str) -> ToolResult:
    # 调用链工具依赖 crg 生命周期(持有 data_dir 与 crg 环境 python),无 CLI 兜底。
    warning = f"{tool_name} unavailable: CRG lifecycle not active"
    return error_result(warning, warnings=[warning])


def make_crg_callers(
    project_root: Path | None, limits: ToolLimits, crg_lifecycle: "CrgLifecycle | None" = None
) -> ToolHandler:
    async def handler(args: dict) -> ToolResult:
        target = str(args.get("target") or "")
        if not target:
            return error_result("crg_callers requires a target")
        if crg_lifecycle is None:
            return _crg_graph_unavailable("crg_callers")
        return await crg_lifecycle.callers(target, _coerce_limit(args.get("limit")))

    return handler


def make_crg_callees(
    project_root: Path | None, limits: ToolLimits, crg_lifecycle: "CrgLifecycle | None" = None
) -> ToolHandler:
    async def handler(args: dict) -> ToolResult:
        target = str(args.get("target") or "")
        if not target:
            return error_result("crg_callees requires a target")
        if crg_lifecycle is None:
            return _crg_graph_unavailable("crg_callees")
        return await crg_lifecycle.callees(target, _coerce_limit(args.get("limit")))

    return handler


def make_crg_affected_flows(
    project_root: Path | None, limits: ToolLimits, crg_lifecycle: "CrgLifecycle | None" = None
) -> ToolHandler:
    async def handler(args: dict) -> ToolResult:
        if crg_lifecycle is None:
            return _crg_graph_unavailable("crg_affected_flows")
        base = args.get("base")
        return await crg_lifecycle.affected_flows(
            str(base) if base else None, _coerce_limit(args.get("limit"))
        )

    return handler


def make_crg_get_flow(
    project_root: Path | None, limits: ToolLimits, crg_lifecycle: "CrgLifecycle | None" = None
) -> ToolHandler:
    async def handler(args: dict) -> ToolResult:
        if crg_lifecycle is None:
            return _crg_graph_unavailable("crg_get_flow")
        flow_name = args.get("flow_name")
        flow_id_raw = args.get("flow_id")
        if not flow_name and flow_id_raw is None:
            return error_result("crg_get_flow requires flow_name or flow_id")
        flow_id: int | None = None
        if flow_id_raw is not None:
            try:
                flow_id = int(flow_id_raw)
            except (TypeError, ValueError):
                return error_result("crg_get_flow flow_id must be an integer")
        return await crg_lifecycle.get_flow(
            str(flow_name) if flow_name else None,
            flow_id,
            _coerce_limit(args.get("limit")),
        )

    return handler
