from __future__ import annotations

import json

import pytest

from cr_agent.tools.catalog import CANONICAL_TOOL_NAMES
from cr_agent.tools.cr_native.registry import CrNativeToolProvider
from cr_agent.tools.facade import ToolFacade, select_tool_provider
from cr_agent.tools.infcode.adapter import InfcodeToolProvider
from cr_agent.tools.provider import ToolSpec
from cr_agent.tools.spec_sdk import to_sdk_tool


# 与 config/agent_tools.toml 一致的 allowlist。
_AGENT_TOOLS = {
    "main": [],
    "context": ["*"],
    "dimension": ["*"],
    "summary": [],
}


def test_select_provider_per_platform() -> None:
    assert isinstance(select_tool_provider("gitlab"), CrNativeToolProvider)
    assert isinstance(select_tool_provider("infcode"), InfcodeToolProvider)


def test_select_provider_rejects_unknown_platform() -> None:
    with pytest.raises(ValueError):
        select_tool_provider("unknown")  # type: ignore[arg-type]


def test_summary_agent_has_no_tools() -> None:
    facade = ToolFacade(provider=CrNativeToolProvider(), agent_tools=_AGENT_TOOLS)
    assert facade.tools_for("summary") == []
    # 未在 allowlist 中声明的 agent 同样拿不到工具。
    assert facade.tools_for("does-not-exist") == []


def test_star_expansion_equals_registry_intersect_provider_gitlab() -> None:
    facade = ToolFacade(provider=CrNativeToolProvider(), agent_tools=_AGENT_TOOLS)
    names = [spec.name for spec in facade.tools_for("context")]
    assert names == list(CANONICAL_TOOL_NAMES)


def test_star_expansion_equals_registry_intersect_provider_infcode() -> None:
    facade = ToolFacade(provider=InfcodeToolProvider(), agent_tools=_AGENT_TOOLS)
    names = [spec.name for spec in facade.tools_for("dimension")]
    # 同一份 allowlist 在 infcode 下 "*" 展开结果与 gitlab 一致(两平台暴露同一全集)。
    assert names == list(CANONICAL_TOOL_NAMES)


def test_star_intersection_drops_names_provider_lacks() -> None:
    # 构造一个只暴露部分工具的 provider,验证 "*" 取交集而非注册表全集。
    class PartialProvider:
        def name(self) -> str:
            return "partial"

        def list_tools(self) -> list[ToolSpec]:
            full = CrNativeToolProvider().list_tools()
            return [s for s in full if s.name in {"read_file", "grep_text"}]

    facade = ToolFacade(provider=PartialProvider(), agent_tools={"context": ["*"]})
    names = [spec.name for spec in facade.tools_for("context")]
    assert names == ["read_file", "grep_text"]


def test_explicit_allowlist_filters_to_requested() -> None:
    facade = ToolFacade(
        provider=CrNativeToolProvider(),
        agent_tools={"context": ["grep_text", "read_file"]},
    )
    names = [spec.name for spec in facade.tools_for("context")]
    # 按注册表顺序返回,read_file 在 grep_text 之前。
    assert names == ["read_file", "grep_text"]


def test_explicit_unknown_tool_is_dropped() -> None:
    facade = ToolFacade(
        provider=CrNativeToolProvider(),
        agent_tools={"context": ["read_file", "nonexistent_tool"]},
    )
    names = [spec.name for spec in facade.tools_for("context")]
    assert names == ["read_file"]


@pytest.mark.asyncio
async def test_placeholder_handlers_return_not_implemented() -> None:
    for provider in (CrNativeToolProvider(), InfcodeToolProvider()):
        spec = next(s for s in provider.list_tools() if s.name == "read_file")
        result = await spec.handler({"path": "x"})
        assert result["ok"] is False
        assert provider.name() in result["error"]
        assert result["warnings"] == []


@pytest.mark.asyncio
async def test_sdk_mapping_wraps_result_into_content_block() -> None:
    spec = next(s for s in CrNativeToolProvider().list_tools() if s.name == "read_file")
    sdk_tool = to_sdk_tool(spec)
    assert sdk_tool.name == "read_file"
    assert sdk_tool.input_schema == spec.input_schema

    sdk_result = await sdk_tool.handler({"path": "x"})
    assert sdk_result["is_error"] is True
    payload = json.loads(sdk_result["content"][0]["text"])
    assert payload["ok"] is False
