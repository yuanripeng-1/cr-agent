from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cr_agent.bootstrap import RuntimeContext
from cr_agent.core.types import QueryResult, TokenUsage
from cr_agent.skills.docs import load_skill_doc
from cr_agent.skills.tool_trace import trace_tools
from cr_agent.tools.provider import ToolResult, ToolSpec
from cr_agent.tools.spec_sdk import to_sdk_tool
from cr_agent.utils.diff import changed_file_payload
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


    '''
        取出 collect_context 需要的工具,用公共 trace_tools 包一层:
        统一打 agent=context 的工具调用日志,并通过 on_call 把每次调用记进 evidence。
    '''
    tools = trace_tools(
        runtime_context.tool_facade.tools_for("context"),
        agent_name="context",
        on_call=lambda tool_name, args, result: evidence.append(
            _evidence_entry(tool_name, args, result)
        ),
    )
    prompt = _build_prompt(runtime_context, tools)
    options = _build_context_options(runtime_context, tools)
    runtime = getattr(runtime_context, "context_runtime", None)
    if runtime is None:
        raise RuntimeError("context runtime is not configured")

    try:
        # Context 子 Agent 启动
        result = await runtime.query_subagent(
            "context",
            prompt,
            assembled_options=options,
            timeout_s=runtime_context.config.timeouts.context_s,
        )
        degraded_warning = ""
    except Exception as exc:
        _logger.warning("DEGRADED reason=CONTEXT_SUBAGENT_FAILED error=%s", exc)
        result = QueryResult(
            text=json.dumps(
                {
                    "summary": "context subagent 失败；使用本地上下文降级结果。",
                    "diff_summary": _local_diff_summary(review_input.diff_content),
                    "warnings": [f"context subagent 失败: {exc}"],
                },
                ensure_ascii=False,
            )
        )
        degraded_warning = f"context subagent 失败: {exc}"
    report = _parse_context_text(result.text)
    warnings = _collect_warnings(evidence, report)
    if degraded_warning and degraded_warning not in warnings:
        warnings.append(degraded_warning)
    artifact = {
        "task_id": review_input.task_id,
        "title": review_input.title,
        "description": review_input.description,
        "commit_messages": review_input.commit_messages,
        "requirements_doc": review_input.requirements_doc,
        "previous_report": review_input.previous_report,
        "project_root": review_input.project_root,
        "platform": runtime_context.platform,
        "raw_diff": {
            "source": "context.json.diff_content",
            "content": review_input.diff_content,
            "diff_file_path": review_input.diff_file_path,
        },
        "changed_files": changed_file_payload(review_input.diff_content),
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

# 构建context subagent的prompt
def _build_prompt(runtime_context: RuntimeContext, tools: list[ToolSpec]) -> str:
    review_input = runtime_context.review_input
    skill_doc = load_skill_doc("collect_context")
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
        "description": review_input.description,
        "commit_messages": review_input.commit_messages,
        "requirements_doc": review_input.requirements_doc,
        "previous_report": review_input.previous_report,
        "changed_files": changed_file_payload(review_input.diff_content),
        "diff_content": review_input.diff_content,
        "available_tools": tool_descriptions,
    }
    return (
        f"{skill_doc}\n\n"
        "你是代码评审的 context subagent。\n"
        "请先理解原始 diff，再动态决定需要使用哪些工具。\n"
        "当语义上下文有帮助时使用 Semble。只有在需要符号/函数调用图时才使用 CRG；"
        "CRG 最好靠后尝试，因为图可能仍在构建中。\n"
        "使用 read_file/read_file_range 收集具体代码证据。\n"
        "如果 Semble 或 CRG 返回 warnings/errors，请基于已有证据继续。\n"
        "返回 JSON，字段为：summary、diff_summary、semantic_context、"
        "call_graph_context、code_snippets、warnings。\n"
        "输入：\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _parse_context_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        return {"summary": "", "warnings": ["context subagent 返回空输出"]}
    json_text = _strip_code_fence(stripped)
    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError:
        return {"summary": stripped, "warnings": ["context subagent 返回非 JSON 摘要"]}
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


def _local_diff_summary(diff_content: str) -> str:
    changed = changed_file_payload(diff_content)
    files = [str(item.get("path") or item.get("new_path") or item) for item in changed]
    if not files:
        return "No changed files parsed from diff."
    preview = ", ".join(files[:20])
    suffix = "" if len(files) <= 20 else f", ... ({len(files)} files total)"
    return f"Changed files: {preview}{suffix}"
