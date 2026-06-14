from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from cr_agent.bootstrap import bootstrap_runtime
from cr_agent.core.types import QueryResult, TokenUsage
from cr_agent.skills.collect_context.skill import collect_context
from cr_agent.tools.provider import ToolSpec, error_result, ok_result


class _FakeFacade:
    def __init__(self, tools: list[ToolSpec]) -> None:
        self.tools = tools
        self.calls: list[str] = []

    def tools_for(self, agent_name: str) -> list[ToolSpec]:
        self.calls.append(agent_name)
        return self.tools


class _ContextRuntime:
    def __init__(self, *, call_crg: bool = True) -> None:
        self.call_crg = call_crg
        self.calls: list[dict[str, Any]] = []

    async def query_subagent(self, agent_name: str, prompt: str, *, assembled_options=None):
        self.calls.append(
            {"agent_name": agent_name, "prompt": prompt, "assembled_options": assembled_options}
        )
        tools = {tool.name: tool for tool in assembled_options._cr_agent_tools}
        await tools["grep_text"].handler({"pattern": "session_id"})
        await tools["semble_search"].handler({"query": "session auxiliary data", "top_k": 3})
        if self.call_crg:
            await tools["crg_query"].handler({"query": "DashboardContent"})
        await tools["read_file_range"].handler(
            {"path": "app.py", "start_line": 1, "end_line": 5}
        )
        return QueryResult(
            text=json.dumps(
                {
                    "summary": "Collected dashboard context.",
                    "diff_summary": "Session aux panel changed.",
                    "semantic_context": [{"source": "semble_search", "summary": "semantic hit"}],
                    "call_graph_context": [{"source": "crg_query", "summary": "call chain"}]
                    if self.call_crg
                    else [],
                    "code_snippets": [{"path": "app.py", "start_line": 1, "end_line": 5}],
                    "warnings": [],
                }
            ),
            usage=TokenUsage(input_tokens=4, output_tokens=5),
        )


def _tool(name: str, result):
    async def handler(args):
        return result(args) if callable(result) else result

    return ToolSpec(
        name=name,
        description=f"{name} fake",
        input_schema={"type": "object", "properties": {}, "additionalProperties": True},
        handler=handler,
    )


def _tools() -> list[ToolSpec]:
    return [
        _tool("grep_text", lambda args: ok_result({"matches": ["app.py:1:session_id"]})),
        _tool("semble_search", lambda args: error_result("semble missing", ["semble missing"])),
        _tool("crg_query", lambda args: error_result("graph not ready", ["graph not ready"])),
        _tool(
            "read_file_range",
            lambda args: ok_result(
                {
                    "path": args["path"],
                    "start_line": args["start_line"],
                    "end_line": args["end_line"],
                    "content": "def handler(): pass",
                }
            ),
        ),
    ]


@pytest.mark.asyncio
async def test_collect_context_writes_stable_artifact_with_provenance(agent_config_path: Path) -> None:
    runtime_context = bootstrap_runtime(agent_config_path, platform_override=None)
    facade = _FakeFacade(_tools())
    context_runtime = _ContextRuntime()
    runtime_context = replace(
        runtime_context,
        tool_facade=facade,
        context_runtime=context_runtime,
    )

    result = await collect_context(runtime_context)

    assert facade.calls == ["context"]
    assert result["artifact_path"] == str(runtime_context.result_dir / "collected_context.json")
    assert "diff_content" not in result
    artifact = json.loads(Path(result["artifact_path"]).read_text(encoding="utf-8"))
    assert artifact["raw_diff"]["source"] == "context.json.diff_content"
    assert artifact["tool_evidence"][0]["tool_name"] == "grep_text"
    assert artifact["semantic_context"][0]["source"] == "semble_search"
    assert artifact["call_graph_context"][0]["source"] == "crg_query"
    assert artifact["code_snippets"][0]["path"] == "app.py"
    assert "semble missing" in artifact["warnings"]
    assert "graph not ready" in artifact["warnings"]


@pytest.mark.asyncio
async def test_collect_context_allows_subagent_to_skip_crg(agent_config_path: Path) -> None:
    runtime_context = bootstrap_runtime(agent_config_path, platform_override=None)
    facade = _FakeFacade(_tools())
    runtime_context = replace(
        runtime_context,
        tool_facade=facade,
        context_runtime=_ContextRuntime(call_crg=False),
    )

    result = await collect_context(runtime_context)
    artifact = json.loads(Path(result["artifact_path"]).read_text(encoding="utf-8"))

    assert result["summary"] == "Collected dashboard context."
    assert artifact["call_graph_context"] == []
    called_tools = [entry["tool_name"] for entry in artifact["tool_evidence"]]
    assert "crg_query" not in called_tools


@pytest.mark.asyncio
async def test_collect_context_returns_context_runtime_usage(agent_config_path: Path) -> None:
    runtime_context = bootstrap_runtime(agent_config_path, platform_override=None)
    runtime_context = replace(
        runtime_context,
        tool_facade=_FakeFacade(_tools()),
        context_runtime=_ContextRuntime(),
    )

    result = await collect_context(runtime_context)

    assert result["usage"]["input_tokens"] == 4
    assert result["usage"]["output_tokens"] == 5
