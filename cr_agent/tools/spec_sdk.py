"""
ToolSpec -> Claude Agent SDK tool 的映射层。

这一层把 ToolSpec 直接转成 SDK 的 SdkMcpTool,使工具契约与 SDK 解耦。
本模块只做纯数据/handler 包装,不创建任何 SDK client、不发起任何网络或模型调用。
SDK 仅在调用映射函数时惰性导入。
"""

from __future__ import annotations

import json
from typing import Any

from cr_agent.tools.provider import ToolResult, ToolSpec


def _wrap_handler(spec: ToolSpec):
    """
    把统一返回结构 {ok, data, warnings, error} 包装成 SDK tool 期望的
    {"content": [...], "is_error": bool}。工具实现本身不感知 SDK。
    """

    async def sdk_handler(args: dict[str, Any]) -> dict[str, Any]:
        result: ToolResult = await spec.handler(args)
        text = json.dumps(result, ensure_ascii=False)
        return {
            "content": [{"type": "text", "text": text}],
            "is_error": not result.get("ok", False),
        }

    return sdk_handler


def to_sdk_tool(spec: ToolSpec):
    """把单个 ToolSpec 转成 SDK 的 SdkMcpTool。"""
    from claude_agent_sdk import SdkMcpTool

    return SdkMcpTool(
        name=spec.name,
        description=spec.description,
        input_schema=spec.input_schema,
        handler=_wrap_handler(spec),
    )


def to_sdk_tools(specs: list[ToolSpec]) -> list[Any]:
    return [to_sdk_tool(spec) for spec in specs]
