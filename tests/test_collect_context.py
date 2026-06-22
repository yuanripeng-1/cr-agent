from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from cr_agent.bootstrap import bootstrap_runtime
from cr_agent.core.types import QueryResult, TokenUsage
from cr_agent.skills.collect_context.skill import (
    _compact_changed_files,
    _parse_context_text,
    collect_context,
)
from cr_agent.tools.provider import ToolSpec, error_result, ok_result


class _FakeFacade:
    def __init__(self, tools: list[ToolSpec]) -> None:
        self.tools = tools
        self.calls: list[str] = []

    def tools_for(self, agent_name: str) -> list[ToolSpec]:
        self.calls.append(agent_name)
        return self.tools


class _ContextRuntime:
    def __init__(self, *, call_crg: bool = True, long_snippet: bool = False) -> None:
        self.call_crg = call_crg
        self.long_snippet = long_snippet
        self.calls: list[dict[str, Any]] = []

    async def query_subagent(self, agent_name: str, prompt: str, *, assembled_options=None, timeout_s=None):
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
                    "code_snippets": [
                        {
                            "path": "app.py",
                            "start_line": 1,
                            "end_line": 5,
                            "content": ("x" * 2000) if self.long_snippet else "def handler(): pass",
                        }
                    ],
                    "warnings": [],
                }
            ),
            usage=TokenUsage(input_tokens=4, output_tokens=5),
        )


class _FailingContextRuntime:
    async def query_subagent(self, agent_name: str, prompt: str, *, assembled_options=None, timeout_s=None):
        raise RuntimeError("context sdk failed")


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
    assert "tool_evidence" not in result
    assert result["diff_summary"] == "Session aux panel changed."
    assert result["semantic_context"] == [{"source": "semble_search", "summary": "semantic hit"}]
    assert result["call_graph_context"] == [{"source": "crg_query", "summary": "call chain"}]
    assert result["code_snippets"][0]["path"] == "app.py"
    assert result["code_snippets"][0]["excerpt"] == "def handler(): pass"
    assert "added_ranges" in result["changed_files"][0]
    assert "added_lines" not in result["changed_files"][0]
    prompt = context_runtime.calls[0]["prompt"]
    assert "## 执行契约" in prompt
    assert "工具失败必须记录为 warnings" in prompt
    assert "collected_context.json" in prompt
    assert "以下是本次运行输入" in prompt
    assert "diff_content" in prompt
    assert "changed_files" in prompt
    assert "available_tools" not in prompt
    artifact = json.loads(Path(result["artifact_path"]).read_text(encoding="utf-8"))
    assert artifact["raw_diff"]["source"] == "context.json.diff_content"
    assert artifact["tool_evidence"][0]["tool_name"] == "grep_text"
    assert artifact["semantic_context"][0]["source"] == "semble_search"
    assert artifact["call_graph_context"][0]["source"] == "crg_query"
    assert artifact["code_snippets"][0]["path"] == "app.py"
    assert "semble missing" in artifact["warnings"]
    assert "graph not ready" in artifact["warnings"]


def test_compact_changed_files_converts_added_lines_to_ranges() -> None:
    result = _compact_changed_files(
        [
            {
                "old_path": "frontend/src/app/page.tsx",
                "new_path": "frontend/src/app/page.tsx",
                "added_lines": [7, 8, 9, 10, 37, 38],
            }
        ]
    )

    assert result == [
        {
            "old_path": "frontend/src/app/page.tsx",
            "new_path": "frontend/src/app/page.tsx",
            "path": "frontend/src/app/page.tsx",
            "added_ranges": [[7, 10], [37, 38]],
        }
    ]


@pytest.mark.asyncio
async def test_collect_context_compacts_downstream_context(agent_config_path: Path) -> None:
    runtime_context = bootstrap_runtime(agent_config_path, platform_override=None)
    runtime_context = replace(
        runtime_context,
        tool_facade=_FakeFacade(_tools()),
        context_runtime=_ContextRuntime(long_snippet=True),
    )

    result = await collect_context(runtime_context)
    snippet = result["code_snippets"][0]

    assert "tool_evidence" not in result
    assert len(snippet["excerpt"]) < 1700
    assert snippet["truncated"] is True
    for changed_file in result["changed_files"]:
        assert "added_ranges" in changed_file
        assert "added_lines" not in changed_file


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


@pytest.mark.asyncio
async def test_collect_context_writes_local_fallback_when_subagent_fails(agent_config_path: Path) -> None:
    runtime_context = bootstrap_runtime(agent_config_path, platform_override=None)
    runtime_context = replace(
        runtime_context,
        tool_facade=_FakeFacade(_tools()),
        context_runtime=_FailingContextRuntime(),
    )

    result = await collect_context(runtime_context)

    assert result["usage"]["input_tokens"] == 0
    assert "context subagent 失败" in result["summary"]
    artifact = json.loads(Path(result["artifact_path"]).read_text(encoding="utf-8"))
    assert artifact["changed_files"]
    assert "context subagent 失败: context sdk failed" in artifact["warnings"]


def test_parse_context_text_extracts_json_from_fenced_and_prose() -> None:
    # 带前后说明文字 + ```json 围栏
    fenced = (
        "这是分析说明，下面是结果：\n"
        "```json\n"
        '{"summary": "ok", "warnings": []}\n'
        "```\n"
        "以上即为输出。"
    )
    assert _parse_context_text(fenced) == {"summary": "ok", "warnings": []}

    # 无围栏但有前缀文字：取首 { 到末 } 兜底
    brace = 'note: here\n{"summary": "spanned"}\ntrailing'
    assert _parse_context_text(brace) == {"summary": "spanned"}

    # 纯非 JSON 文本：回退为摘要 + warning
    plain = _parse_context_text("just a plain summary")
    assert plain["summary"] == "just a plain summary"
    assert plain["warnings"] == ["context subagent 返回非 JSON 摘要"]


class _CrgCallersRuntime:
    """调用 crg_callers 但不在 report 中显式给出 call_graph_context。"""

    async def query_subagent(self, agent_name: str, prompt: str, *, assembled_options=None, timeout_s=None):
        tools = {tool.name: tool for tool in assembled_options._cr_agent_tools}
        await tools["crg_callers"].handler({"target": "pkg/x.go::Foo"})
        return QueryResult(
            text=json.dumps(
                {
                    "summary": "graph collected",
                    "diff_summary": "changed",
                    # 故意不提供 call_graph_context，触发 evidence 回退
                    "warnings": [],
                }
            ),
            usage=TokenUsage(input_tokens=1, output_tokens=1),
        )


@pytest.mark.asyncio
async def test_call_graph_context_falls_back_to_any_crg_tool(agent_config_path: Path) -> None:
    runtime_context = bootstrap_runtime(agent_config_path, platform_override=None)
    crg_tools = [
        _tool("crg_callers", lambda args: ok_result({"results": [{"caller": "Bar"}]})),
    ]
    runtime_context = replace(
        runtime_context,
        tool_facade=_FakeFacade(crg_tools),
        context_runtime=_CrgCallersRuntime(),
    )

    result = await collect_context(runtime_context)
    artifact = json.loads(Path(result["artifact_path"]).read_text(encoding="utf-8"))

    captured = artifact["call_graph_context"]
    assert captured, "crg_callers 调用应回退进 call_graph_context"
    assert captured[0]["tool_name"] == "crg_callers"
