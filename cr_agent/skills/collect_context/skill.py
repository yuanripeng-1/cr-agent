from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cr_agent.bootstrap import RuntimeContext
from cr_agent.core.types import TokenUsage
from cr_agent.tools.provider import ToolResult, ToolSpec
from cr_agent.tools.spec_sdk import to_sdk_tool
from cr_agent.utils.logging import get_logger

_logger = get_logger("cr_agent.skills.collect_context")


async def collect_context(runtime_context: RuntimeContext) -> dict[str, Any]:
    """
    启动 context subagent 收集上下文。

    工具只从 facade.tools_for("context") 获取。Python 不预判 Semble/CRG query,
    由 context subagent 根据 diff 和前序工具结果动态决定是否调用。
    """
    review_input = runtime_context.review_input
    artifact_path = runtime_context.result_dir / "collected_context.json"
    evidence: list[dict[str, Any]] = []
    tools = _trace_tools(runtime_context.tool_facade.tools_for("context"), evidence)
    prompt = _build_prompt(runtime_context, tools)
    options = _build_context_options(runtime_context, tools)
    runtime = getattr(runtime_context, "context_runtime", None)
    if runtime is None:
        raise RuntimeError("context runtime is not configured")

    result = await runtime.query_subagent(
        "context",
        prompt,
        assembled_options=options,
    )
    report = _parse_context_text(result.text)
    warnings = _collect_warnings(evidence, report)
    artifact = {
        "task_id": review_input.task_id,
        "title": review_input.title,
        "project_root": review_input.project_root,
        "platform": runtime_context.platform,
        "raw_diff": {
            "source": "context.json.diff_content",
            "content": review_input.diff_content,
            "diff_file_path": review_input.diff_file_path,
        },
        "diff_summary": report.get("diff_summary", ""),
        "summary": report.get("summary", ""),
        "tool_evidence": evidence,
        "semantic_context": _section(report, "semantic_context", "semble_search", evidence),
        "call_graph_context": _section(report, "call_graph_context", "crg_query", evidence),
        "code_snippets": _code_snippets(report, evidence),
        "warnings": warnings,
    }
    _write_json(artifact_path, artifact)
    _logger.info("ARTIFACT_WRITE path=%s", artifact_path)
    return {
        "task_id": review_input.task_id,
        "artifact_path": str(artifact_path),
        "summary": artifact["summary"] or artifact["diff_summary"],
        "warnings": warnings,
        "usage": _usage_dict(result.usage),
    }


def _trace_tools(tools: list[ToolSpec], evidence: list[dict[str, Any]]) -> list[ToolSpec]:
    traced: list[ToolSpec] = []
    for tool in tools:
        original_handler = tool.handler

        async def traced_handler(args: dict[str, Any], *, _tool=tool, _handler=original_handler):
            result: ToolResult = await _handler(args)
            evidence.append(_evidence_entry(_tool.name, args, result))
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


def _build_context_options(runtime_context: RuntimeContext, tools: list[ToolSpec]) -> Any:
    if not tools:
        return None
    from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server

    from cr_agent.core.sdk_runtime import build_sdk_env

    server = create_sdk_mcp_server(name="context", tools=[to_sdk_tool(t) for t in tools])
    options = ClaudeAgentOptions(
        model=runtime_context.config.llm.model,
        env=build_sdk_env(runtime_context.config.llm),
        mcp_servers={"context": server},
        allowed_tools=[f"mcp__context__{tool.name}" for tool in tools],
        tools=[],
    )
    # 供测试 fake runtime 与本地 instrumentation 读取;真实 SDK 忽略这个动态属性。
    options._cr_agent_tools = tools  # type: ignore[attr-defined]
    return options


def _build_prompt(runtime_context: RuntimeContext, tools: list[ToolSpec]) -> str:
    review_input = runtime_context.review_input
    tool_descriptions = [
        {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
        for tool in tools
    ]
    payload = {
        "task_id": review_input.task_id,
        "title": review_input.title,
        "project_root": review_input.project_root,
        "source_branch": review_input.source_branch,
        "target_branch": review_input.target_branch,
        "diff_content": review_input.diff_content,
        "available_tools": tool_descriptions,
    }
    return (
        "You are the context subagent for a code review.\n"
        "First understand the raw diff. Dynamically decide which tools to use.\n"
        "Use Semble for semantic context when useful. Use CRG only when you need a "
        "symbol/function call graph; CRG is best tried late because the graph may still build.\n"
        "Use read_file/read_file_range to collect concrete code evidence.\n"
        "If Semble or CRG returns warnings/errors, continue with available evidence.\n"
        "Return JSON with keys: summary, diff_summary, semantic_context, "
        "call_graph_context, code_snippets, warnings.\n"
        "Input:\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _parse_context_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        return {"summary": "", "warnings": ["context subagent returned empty output"]}
    json_text = _strip_code_fence(stripped)
    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError:
        return {"summary": stripped, "warnings": ["context subagent returned non-json summary"]}
    return parsed if isinstance(parsed, dict) else {"summary": stripped}


def _strip_code_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) >= 3 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return text


def _evidence_entry(tool_name: str, args: dict[str, Any], result: ToolResult) -> dict[str, Any]:
    return {
        "source": "context_subagent_tool_call",
        "tool_name": tool_name,
        "input": args,
        "ok": bool(result.get("ok")),
        "warnings": result.get("warnings") or [],
        "error": result.get("error"),
        "summary": _summarize_tool_result(result),
        "data": result.get("data"),
    }


def _summarize_tool_result(result: ToolResult) -> str:
    if not result.get("ok"):
        return str(result.get("error") or "tool failed")
    data = result.get("data")
    if isinstance(data, dict):
        for key in ("matches", "results", "lines"):
            value = data.get(key)
            if isinstance(value, list):
                return f"{len(value)} {key}"
        if "content" in data:
            return f"content bytes={len(str(data.get('content') or ''))}"
    return "tool succeeded"


def _section(report: dict[str, Any], key: str, tool_name: str, evidence: list[dict[str, Any]]) -> list[Any]:
    explicit = report.get(key)
    if isinstance(explicit, list):
        return explicit
    return [entry for entry in evidence if entry.get("tool_name") == tool_name]


def _code_snippets(report: dict[str, Any], evidence: list[dict[str, Any]]) -> list[Any]:
    explicit = report.get("code_snippets")
    if isinstance(explicit, list):
        return explicit
    return [
        entry
        for entry in evidence
        if entry.get("tool_name") in {"read_file", "read_file_range"}
    ]


def _collect_warnings(evidence: list[dict[str, Any]], report: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for item in report.get("warnings") or []:
        warnings.append(str(item))
    for entry in evidence:
        for warning in entry.get("warnings") or []:
            warnings.append(str(warning))
        if entry.get("ok") is False and entry.get("error"):
            warnings.append(str(entry["error"]))
    return warnings


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _usage_dict(usage: TokenUsage) -> dict[str, int]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
    }
