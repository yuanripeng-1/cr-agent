from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from cr_agent.core.sdk_runtime import build_sdk_env
from cr_agent.issue_resolution.bootstrap import IssueResolutionRuntimeContext
from cr_agent.issue_resolution.contracts import (
    IssueResolutionItemResult,
    normalize_agent_results,
)
from cr_agent.tools.provider import ToolSpec
from cr_agent.tools.spec_sdk import to_sdk_tool
from cr_agent.utils.logging import get_logger

_logger = get_logger("cr_agent.issue_resolution.agent")

_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompt" / "issue_resolution.md"


def load_resolution_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8").strip()


def _build_resolution_options(
    runtime_context: IssueResolutionRuntimeContext,
    tools: list[ToolSpec],
) -> Any:
    if not tools:
        return None
    from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server

    server = create_sdk_mcp_server(
        name="resolution",
        tools=[to_sdk_tool(tool) for tool in tools],
    )
    options = ClaudeAgentOptions(
        model=runtime_context.config.llm.model,
        env=build_sdk_env(runtime_context.config.llm),
        mcp_servers={"resolution": server},
        allowed_tools=[f"mcp__resolution__{tool.name}" for tool in tools],
        tools=[],
    )
    options._cr_agent_tools = tools  # type: ignore[attr-defined]
    return options


def _build_prompt(runtime_context: IssueResolutionRuntimeContext) -> str:
    resolution_input = runtime_context.resolution_input
    payload = {
        "task_id": resolution_input.task_id,
        "project_id": resolution_input.project_id,
        "mr_iid": resolution_input.mr_iid,
        "mr_url": resolution_input.mr_url,
        "source_branch": resolution_input.source_branch,
        "target_branch": resolution_input.target_branch,
        "merged_commit_sha": resolution_input.merged_commit_sha,
        "project_root": str(runtime_context.project_root),
        "issues": [issue.model_dump() for issue in resolution_input.issues],
    }
    return (
        f"{load_resolution_prompt()}\n\n"
        "## 输入\n\n"
        f"```json\n{json.dumps(payload, ensure_ascii=False, indent=2)}\n```"
    )


def _isolate_json(text: str) -> str:
    stripped = text.strip()
    fence_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", stripped, re.IGNORECASE)
    if fence_match:
        return fence_match.group(1).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start >= 0 and end > start:
        return stripped[start : end + 1]
    return stripped


def parse_resolution_response(
    text: str,
    *,
    known_line_review_ids: set[int],
) -> list[IssueResolutionItemResult]:
    stripped = text.strip()
    if not stripped:
        raise ValueError("resolution agent returned empty output")

    json_text = _isolate_json(stripped)
    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"resolution agent returned invalid json: {exc}") from exc

    if not isinstance(parsed, dict):
        raise ValueError("resolution agent json root must be an object")

    raw_results = parsed.get("results", [])
    if not isinstance(raw_results, list):
        raise ValueError("resolution agent json.results must be a list")

    normalized = normalize_agent_results(
        raw_results,
        known_line_review_ids=known_line_review_ids,
    )
    if not normalized and raw_results:
        raise ValueError("resolution agent returned no valid result items")
    return normalized


async def run_resolution_agent(
    runtime_context: IssueResolutionRuntimeContext,
) -> list[IssueResolutionItemResult]:
    if not runtime_context.resolution_input.issues:
        _logger.info("ISSUE_RESOLUTION_SKIP reason=no_open_issues")
        return []

    tools = runtime_context.tool_facade.tools_for("resolution")
    prompt = _build_prompt(runtime_context)
    options = _build_resolution_options(runtime_context, tools)
    runtime = runtime_context.resolution_runtime
    if runtime is None:
        raise RuntimeError("resolution runtime is not configured")

    known_ids = {
        issue.line_review_id for issue in runtime_context.resolution_input.issues
    }
    timeout_s = getattr(runtime_context.config.timeouts, "dimension_s", 300.0)

    _logger.info(
        "ISSUE_RESOLUTION_AGENT_START issues=%s tools=%s",
        len(runtime_context.resolution_input.issues),
        [tool.name for tool in tools],
    )
    response = await runtime.query_subagent(
        "resolution",
        prompt,
        assembled_options=options,
        timeout_s=timeout_s,
    )
    _logger.info(
        "ISSUE_RESOLUTION_AGENT_DONE text_chars=%s",
        len(response.text or ""),
    )
    return parse_resolution_response(
        response.text,
        known_line_review_ids=known_ids,
    )
