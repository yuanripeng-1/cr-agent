"""
子 agent 工具调用的统一 tracing。

所有需要把工具暴露给子 agent 的 skill(collect_context / dimension_review / ...)
都用 trace_tools() 包一层,保证 run.log 里每次工具调用都带:
  - 是哪个子 agent 发起(agent=...)
  - 调用参数(args=...,长字符串截断)
  - 结果是否成功 / 耗时 / warning 数 / 结果摘要
  - 失败(ok=False)与异常分别落 WARNING / ERROR,便于事后定位

工具实现仍在 tools/cr_native;本模块只做日志包装,不改变 handler 行为。
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable

from cr_agent.tools.provider import ToolResult, ToolSpec
from cr_agent.utils.logging import get_logger

_logger = get_logger("cr_agent.skills.tool_trace")

# on_call 在每次工具返回后被调用(成功或降级都会调),供 skill 收集 evidence 等。
OnCall = Callable[[str, dict[str, Any], ToolResult], None]


def summarize_args(args: dict[str, Any]) -> dict[str, Any]:
    """把工具入参整理成可安全落盘的摘要:长字符串截断,其它原样。"""
    summary: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str):
            summary[key] = value if len(value) <= 160 else value[:160] + "...[truncated]"
        else:
            summary[key] = value
    return summary


def summarize_result(result: ToolResult) -> str:
    """对工具结果做一句话摘要,帮助判断“调到了但没拿到东西”。"""
    if not result.get("ok"):
        return str(result.get("error") or "tool failed")
    data = result.get("data")
    if isinstance(data, dict):
        for key in ("matches", "results", "lines"):
            value = data.get(key)
            if isinstance(value, list):
                return f"{key}={len(value)}"
        if "content" in data:
            return f"content_bytes={len(str(data.get('content') or ''))}"
    return "ok"


def trace_tools(
    tools: list[ToolSpec],
    *,
    agent_name: str,
    on_call: OnCall | None = None,
) -> list[ToolSpec]:
    """
    返回包装后的 ToolSpec 列表:handler 外面套一层统一日志/计时。

    agent_name 例如 "context" / "dimension:security",会出现在每条工具日志里。
    on_call 可选,用于在工具返回后让 skill 追加 evidence(不影响日志)。
    """
    traced: list[ToolSpec] = []
    for tool in tools:
        original_handler = tool.handler

        async def traced_handler(
            args: dict[str, Any],
            *,
            _tool: ToolSpec = tool,
            _handler: Callable[[dict[str, Any]], Awaitable[ToolResult]] = original_handler,
        ) -> ToolResult:
            start = time.monotonic()
            _logger.info(
                "TOOL_CALL_START agent=%s tool=%s args=%s",
                agent_name,
                _tool.name,
                summarize_args(args),
            )
            try:
                result: ToolResult = await _handler(args)
            except Exception as exc:
                elapsed_ms = int((time.monotonic() - start) * 1000)
                # handler 通常会把异常降级成 error_result;真抛出说明是包装层外的意外。
                _logger.error(
                    "TOOL_CALL_EXCEPTION agent=%s tool=%s ms=%s error=%s",
                    agent_name,
                    _tool.name,
                    elapsed_ms,
                    exc,
                )
                raise

            elapsed_ms = int((time.monotonic() - start) * 1000)
            warnings = result.get("warnings") or []
            if result.get("ok"):
                _logger.info(
                    "TOOL_CALL_END agent=%s tool=%s ok=True ms=%s warnings=%s result=%s",
                    agent_name,
                    _tool.name,
                    elapsed_ms,
                    len(warnings),
                    summarize_result(result),
                )
            else:
                _logger.warning(
                    "TOOL_CALL_FAILED agent=%s tool=%s ms=%s warnings=%s error=%s",
                    agent_name,
                    _tool.name,
                    elapsed_ms,
                    len(warnings),
                    result.get("error"),
                )

            if on_call is not None:
                on_call(_tool.name, args, result)
            return result

        traced.append(
            ToolSpec(
                name=tool.name,
                description=tool.description,
                input_schema=tool.input_schema,
                handler=traced_handler,
            )
        )
    return traced
