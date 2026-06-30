"""
cr-native 本地文件工具实现。

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

from typing import TYPE_CHECKING

from cr_agent.tools.provider import ToolHandler, ToolResult, error_result, ok_result
from cr_agent.utils.logging import get_logger

if TYPE_CHECKING:
    from cr_agent.core.agent_config import AgentConfig

_logger = get_logger("cr_agent.tools.cr_native")


@dataclass(frozen=True)
class ToolLimits:
    # 单次工具调用的超时(秒)。
    timeout_s: float = 30.0
    # semble_search 专用超时(秒);冷启动加载模型较慢。
    semble_timeout_s: float = 120.0
    # read_file 最大读取字节,超出截断并 warning。
    max_file_bytes: int = 1_000_000
    # read_file_range 单次最大返回行数。
    read_file_range_max_lines: int = 120
    # read_file_range 单次最大返回 content 字节。
    read_file_range_max_content_bytes: int = 8 * 1024
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


def _resolve_existing(project_root: Path, raw: str) -> Path:
    """
    在 safe_resolve 基础上做相对路径容错。

    模型有时会把 path 写成带 project_root 名(如 `project_code/`)的相对路径,
    例如 `workspace/<task>/project_code/frontend/src/app/page.tsx`,直接拼到
    project_root 后会变成双重前缀而找不到文件。这里在直连结果不存在时,尝试剥掉
    到最后一次 `<project_root.name>/` 之前的内容再解析一次;仅接受仍在 root 内的结果。
    """
    primary = safe_resolve(project_root, raw)
    if primary.exists():
        return primary

    root = project_root.resolve()
    marker = f"{root.name}/"
    idx = raw.rfind(marker)
    if idx != -1:
        tail = raw[idx + len(marker):]
        if tail:
            try:
                alt = safe_resolve(project_root, tail)
            except PathEscapeError:
                return primary
            if alt.exists():
                return alt
    return primary


def _read_text_blocking(path: Path, max_bytes: int) -> tuple[str, bool]:
    """阻塞读取(放进线程执行)。返回 (文本, 是否被截断)。"""
    data = path.read_bytes()
    truncated = len(data) > max_bytes
    if truncated:
        data = data[:max_bytes]
    return data.decode("utf-8", errors="replace"), truncated


def _truncate_text_bytes(text: str, max_bytes: int) -> tuple[str, bool]:
    """按 UTF-8 字节截断文本,避免切坏多字节字符。"""
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text, False
    return data[:max_bytes].decode("utf-8", errors="ignore"), True


async def _guard(coro, *, tool_name: str, timeout_s: float) -> ToolResult:
    """统一超时与日志包装。超时/异常都转成降级结构,不抛穿主流程。

    START/END 走 DEBUG:上层 skills.tool_trace 已经在 INFO 打了带 agent+args 的
    工具调用日志,这里再打 INFO 会重复。但超时/异常仍走 ERROR——它带的是真实底层
    原因(timeout / 具体异常),是上层 TOOL_CALL_FAILED 的根因,必须在 run.log 可见。
    """
    start = time.monotonic()
    _logger.debug("TOOL_GUARD_START tool=%s", tool_name)
    try:
        result: ToolResult = await asyncio.wait_for(coro, timeout=timeout_s)
    except asyncio.TimeoutError:
        _logger.error("TOOL_CALL_ERROR tool=%s reason=timeout timeout_s=%s", tool_name, timeout_s)
        return error_result(f"{tool_name} timed out after {timeout_s}s")
    except Exception as exc:  # 工具内部异常一律降级,主流程继续。
        _logger.error("TOOL_CALL_ERROR tool=%s reason=%s", tool_name, exc)
        return error_result(f"{tool_name} failed: {exc}")
    elapsed_ms = int((time.monotonic() - start) * 1000)
    _logger.debug("TOOL_GUARD_END tool=%s ok=%s ms=%s", tool_name, result.get("ok"), elapsed_ms)
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
            raw = str(args.get("path", ""))
            target = _resolve_existing(project_root, raw)
            if not target.is_file():
                return error_result(
                    f"not a file: {raw} (resolved={target}); "
                    "path 应为相对 project_root 的路径"
                )
            text, truncated = await asyncio.to_thread(
                _read_text_blocking, target, limits.max_file_bytes
            )
            warnings = (
                [f"file truncated to {limits.max_file_bytes} bytes"] if truncated else []
            )
            return ok_result({"path": raw, "content": text}, warnings)

        return await _guard(work(), tool_name="read_file", timeout_s=limits.timeout_s)

    return handler


def make_read_file_range(project_root: Path | None, limits: ToolLimits) -> ToolHandler:
    async def handler(args: dict) -> ToolResult:
        guard = _require_root(project_root, "read_file_range")
        if guard is not None:
            return guard

        async def work() -> ToolResult:
            raw = str(args.get("path", ""))
            target = _resolve_existing(project_root, raw)
            if not target.is_file():
                return error_result(
                    f"not a file: {raw} (resolved={target}); "
                    "path 应为相对 project_root 的路径"
                )
            start_line = int(args.get("start_line", 1))
            end_line = int(args.get("end_line", start_line))
            if start_line < 1 or end_line < start_line:
                return error_result("invalid line range")

            text, truncated = await asyncio.to_thread(
                _read_text_blocking, target, limits.max_file_bytes
            )
            lines = text.splitlines()
            # 行号 1-based,闭区间。
            requested_lines = end_line - start_line + 1
            capped_end_line = end_line
            warnings = (
                [f"file truncated to {limits.max_file_bytes} bytes"] if truncated else []
            )
            if requested_lines > limits.read_file_range_max_lines:
                capped_end_line = start_line + limits.read_file_range_max_lines - 1
                warnings.append(
                    "line range truncated to "
                    f"{limits.read_file_range_max_lines} lines"
                )
            selected = lines[start_line - 1 : capped_end_line]
            content = "\n".join(selected)
            content, byte_truncated = _truncate_text_bytes(
                content,
                limits.read_file_range_max_content_bytes,
            )
            if byte_truncated:
                warnings.append(
                    "content truncated to "
                    f"{limits.read_file_range_max_content_bytes} bytes"
                )
            return ok_result(
                {
                    "path": raw,
                    "start_line": start_line,
                    "end_line": capped_end_line,
                    "requested_end_line": end_line,
                    "content": content,
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
            else:
                # 无显式 path 时显式给 "."(cwd=root)。否则 rg 在 stdin 为管道时
                # 会去读 stdin 而非搜索目录,直接阻塞到工具超时。
                cmd.append(".")

            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    cwd=str(root),
                    # 兜底:即便 rg 因故仍尝试读 stdin,也立刻拿到 EOF 而非挂起。
                    stdin=asyncio.subprocess.DEVNULL,
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


def tool_limits_from_config(config: AgentConfig) -> ToolLimits:
    return ToolLimits(semble_timeout_s=config.tools.semble.timeout_s)
