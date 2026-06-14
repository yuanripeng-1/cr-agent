from __future__ import annotations

import json
from typing import Any

from cr_agent.bootstrap import RuntimeContext
from cr_agent.core.errors import RuntimeCallError
from cr_agent.utils.logging import get_logger

_logger = get_logger("cr_agent.skills.summarize")


async def summarize_report(
    runtime_context: RuntimeContext,
    collected_context: dict[str, Any],
    dimension_scores: list[dict[str, Any]],
    validation_errors: list[str] | None = None,
) -> dict[str, Any]:
    """
    调 summary subagent 生成最终报告。

    summary agent 默认无工具;这里只通过 runtime.query_subagent("summary") 发起模型调用。
    输出优先按 JSON 解析,早期模型只返回 Markdown 时降级包装为 llm_result。
    """
    runtime = getattr(runtime_context, "summary_runtime", None)
    if runtime is None:
        runtime = getattr(runtime_context, "runtime", None)
    if runtime is None:
        raise RuntimeCallError("summary runtime is not configured")

    prompt = _build_summary_prompt(collected_context, dimension_scores, validation_errors or [])
    result = await runtime.query_subagent(
        "summary",
        prompt,
        assembled_options=None,
    )
    report = _parse_summary_text(result.text)
    report["validation_errors"] = validation_errors or []
    return report


def _build_summary_prompt(
    collected_context: dict[str, Any],
    dimension_scores: list[dict[str, Any]],
    validation_errors: list[str],
) -> str:
    payload = {
        "collected_context": collected_context,
        "dimension_scores": dimension_scores,
        "validation_errors": validation_errors,
    }
    return (
        "Generate the final code review report as JSON.\n"
        "Required field: llm_result (non-empty Markdown string).\n"
        "Optional fields: line_comments {comments: []}, issues [].\n"
        "If validation_errors is non-empty, fix those schema errors.\n"
        "Input:\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _parse_summary_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        return {"llm_result": ""}

    json_text = _strip_code_fence(stripped)
    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError:
        _logger.info("SUMMARY_PARSE_FALLBACK reason=non_json")
        return {"llm_result": stripped}
    if isinstance(parsed, dict):
        return parsed
    _logger.info("SUMMARY_PARSE_FALLBACK reason=json_not_object")
    return {"llm_result": stripped}


def _strip_code_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) >= 3 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return text
