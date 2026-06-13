"""
cr-native 本地文件工具实现(PR4)。

实现 read_file / read_file_range / glob_files / grep_text 四个工具的 handler 工厂。
约束:
- 文件工具严格限制在 project_root 下,路径规范化 + symlink 逃逸防护(safe_resolve)。
- 每个工具有超时、输出大小上限、统一结构化返回 {ok, data, warnings, error}。
- grep_text 走系统 rg;rg 缺失返回降级错误,不抛穿主流程。

工具实现只在这里,不写进 skill。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path

from cr_agent.tools.provider import ToolHandler, ToolResult, error_result, ok_result
from cr_agent.utils.logging import get_logger

_logger = get_logger("cr_agent.tools.cr_native")


@dataclass(frozen=True)
class ToolLimits:
    # 单次工具调用的超时(秒)。
    timeout_s: float = 30.0
    # read_file 最大读取字节,超出截断并 warning。
    max_file_bytes: int = 1_000_000
    # grep 输出最大字节,超出截断并 warning。
    max_grep_bytes: int = 200_000
    # glob 返回的最大条目数。
    max_glob_results: int = 1000


class PathEscapeError(Exception):
    """请求路径越出 project_root。"""


def safe_resolve(project_root: Path, raw: str) -> Path:
    """
    把 raw 当作 project_root 下的相对路径解析,规范化 `..` 并跟随 symlink。
    解析结果必须仍在 project_root 内,否则抛 PathEscapeError。
    绝对路径入参会落到 root 外,从而被拒绝。
    """
    root = project_root.resolve()
    candidate = (root / raw).resolve()
    if candidate != root and not candidate.is_relative_to(root):
        raise PathEscapeError(f"path escapes project_root: {raw}")
    return candidate


def _read_text_blocking(path: Path, max_bytes: int) -> tuple[str, bool]:
    """阻塞读取(放进线程执行)。返回 (文本, 是否被截断)。"""
    data = path.read_bytes()
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    return data.decode("utf-8", errors="replace"), truncated


async def _guard(coro, *, tool_name: str, timeout_s: float) -> ToolResult:
    """统一超时与日志包装。超时/异常都转成降级结构,不抛穿主流程。"""
    start = time.monotonic()
    _logger.info("TOOL_CALL_START tool=%s", tool_name)
    try:
        result: ToolResult = await asyncio.wait_for(coro, timeout=timeout_s)
    except asyncio.TimeoutError:
        _logger.error("TOOL_CALL_ERROR tool=%s reason=timeout", tool_name)
        return error_result(f"{tool_name} timed out after {timeout_s}s")
    except Exception as exc:  # 工具内部异常一律降级,主流程继续。
        _logger.error("TOOL_CALL_ERROR tool=%s reason=%s", tool_name, exc)
        return error_result(f"{tool_name} failed: {exc}")
    elapsed_ms = int((time.monotonic() - start) * 1000)
    _logger.info("TOOL_CALL_END tool=%s ok=%s ms=%s", tool_name, result.get("ok"), elapsed_ms)
    return result


def _require_root(project_root: Path | None, tool_name: str) -> ToolResult | None:
    if project_root is None:
        return error_result(f"{tool_name} unavailable: project_root not configured")
    if not project_root.exists():
        return error_result(f"{tool_name} unavailable: project_root does not exist")
    return None


def make_read_file(project_root: Path | None, limits: ToolLimits) -> ToolHandler:
    async def handler(args: dict) -> ToolResult:
        guard = _require_root(project_root, "read_file")
        if guard is not None:
            return guard

        async def work() -> ToolResult:
            target = safe_resolve(project_root, str(args.get("path", "")))
            if not target.is_file():
                return error_result(f"not a file: {args.get('path', '')}")
            text, truncated = await asyncio.to_thread(
                _read_text_blocking, target, limits.max_file_bytes
            )
            warnings = (
                [f"file truncated to {limits.max_file_bytes} bytes"] if truncated else []
            )
            return ok_result({"path": str(args.get("path", "")), "content": text}, warnings)

        return await _guard(work(), tool_name="read_file", timeout_s=limits.timeout_s)

    return handler


def make_read_file_range(project_root: Path | None, limits: ToolLimits) -> ToolHandler:
    async def handler(args: dict) -> ToolResult:
        guard = _require_root(project_root, "read_file_range")
        if guard is not None:
            return guard

        async def work() -> ToolResult:
            target = safe_resolve(project_root, str(args.get("path", "")))
            if not target.is_file():
                return error_result(f"not a file: {args.get('path', '')}")
            start_line = int(args.get("start_line", 1))
            end_line = int(args.get("end_line", start_line))
            if start_line < 1 or end_line < start_line:
                return error_result("invalid line range")

            text, truncated = await asyncio.to_thread(
                _read_text_blocking, target, limits.max_file_bytes
            )
            lines = text.splitlines()
            # 行号 1-based,闭区间。
            selected = lines[start_line - 1 : end_line]
            warnings = (
                [f"file truncated to {limits.max_file_bytes} bytes"] if truncated else []
            )
            return ok_result(
                {
                    "path": str(args.get("path", "")),
                    "start_line": start_line,
                    "end_line": end_line,
                    "content": "\n".join(selected),
                },
                warnings,
            )

        return await _guard(work(), tool_name="read_file_range", timeout_s=limits.timeout_s)

    return handler


def make_glob_files(project_root: Path | None, limits: ToolLimits) -> ToolHandler:
    async def handler(args: dict) -> ToolResult:
        guard = _require_root(project_root, "glob_files")
        if guard is not None:
            return guard

        async def work() -> ToolResult:
            root = project_root.resolve()
            # root 可由 args.root 指定子目录,但仍须在 project_root 内。
            base = safe_resolve(project_root, str(args.get("root", "."))) if args.get("root") else root
            pattern = str(args.get("pattern", "*"))
            matches: list[str] = []
            warnings: list[str] = []
            for entry in base.glob(pattern):
                resolved = entry.resolve()
                # 过滤 symlink 等逃逸到 project_root 外的条目。
                if resolved != root and not resolved.is_relative_to(root):
                    continue
                matches.append(str(resolved.relative_to(root)))
                if len(matches) >= limits.max_glob_results:
                    warnings.append(f"glob truncated to {limits.max_glob_results} results")
                    break
            matches.sort()
            return ok_result({"pattern": pattern, "matches": matches}, warnings)

        return await _guard(work(), tool_name="glob_files", timeout_s=limits.timeout_s)

    return handler


def make_grep_text(project_root: Path | None, limits: ToolLimits) -> ToolHandler:
    async def handler(args: dict) -> ToolResult:
        guard = _require_root(project_root, "grep_text")
        if guard is not None:
            return guard

        async def work() -> ToolResult:
            root = project_root.resolve()
            pattern = str(args.get("pattern", ""))
            if not pattern:
                return error_result("grep_text requires a pattern")
            cmd = ["rg", "--line-number", "--no-heading", "--color", "never", pattern]
            glob = args.get("glob")
            if glob:
                cmd += ["--glob", str(glob)]
            search_path = args.get("path")
            if search_path:
                # path 仍须在 project_root 内。
                safe_resolve(project_root, str(search_path))
                cmd.append(str(search_path))

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(root),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except FileNotFoundError:
                # rg 缺失:降级,不抛穿主流程。
                return error_result("ripgrep (rg) not available")

            try:
                stdout, _stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=limits.timeout_s
                )
            except asyncio.TimeoutError:
                # 超时:杀掉 rg 进程,避免孤儿;返回降级结构。
                proc.kill()
                await proc.wait()
                return error_result(f"grep_text timed out after {limits.timeout_s}s")
            warnings: list[str] = []
            if len(stdout) > limits.max_grep_bytes:
                stdout = stdout[: limits.max_grep_bytes]
                warnings.append(f"grep output truncated to {limits.max_grep_bytes} bytes")
            text = stdout.decode("utf-8", errors="replace")
            matches = [line for line in text.splitlines() if line]
            # rg 退出码 1 表示无匹配,不是错误。
            return ok_result({"pattern": pattern, "matches": matches}, warnings)

        return await _guard(work(), tool_name="grep_text", timeout_s=limits.timeout_s)

    return handler
