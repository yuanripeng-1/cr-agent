"""
工具层契约。

ToolSpec 直接对齐 Claude Agent SDK `tool` 的约定:
(name, description, input_schema, handler)。input_schema 使用 JSON Schema
dict,无需重构即可经 spec_sdk.to_sdk_tool 转成 SDK 工具。

工具统一返回结构 {ok, data, warnings, error};SDK content-block
包装由映射层在调用边界完成,工具实现本身不感知 SDK。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Protocol, runtime_checkable

# 工具入参 schema 采用 JSON Schema dict,与 SDK tool 的 input_schema 一致。
JsonSchema = dict[str, Any]
# 工具统一返回 {ok, data, warnings, error}。
ToolResult = dict[str, Any]
ToolHandler = Callable[[dict[str, Any]], Awaitable[ToolResult]]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    # JSON Schema,对齐 SDK tool 的 input_schema 约定。
    input_schema: JsonSchema
    handler: ToolHandler


@runtime_checkable
class ToolProvider(Protocol):
    def name(self) -> str:
        ...

    def list_tools(self) -> list[ToolSpec]:
        ...


def ok_result(data: Any = None, warnings: list[str] | None = None) -> ToolResult:
    return {"ok": True, "data": data, "warnings": warnings or [], "error": None}


def error_result(error: str, warnings: list[str] | None = None) -> ToolResult:
    return {"ok": False, "data": None, "warnings": warnings or [], "error": error}


def not_implemented_result(tool_name: str, provider: str) -> ToolResult:
    """工具未在该 provider 接入真实能力时的统一降级返回。"""
    return error_result(f"{tool_name} not implemented in {provider} provider")
