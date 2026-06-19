from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cr_agent.bootstrap import RuntimeContext
from cr_agent.core.errors import RuntimeCallError
from cr_agent.skills.docs import load_skill_doc
from cr_agent.utils.logging import get_logger

_logger = get_logger("cr_agent.skills.summarize")
_PROMPT_DIR = Path(__file__).resolve().parents[3] / "prompt"


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

    prompt = _build_summary_prompt(runtime_context, collected_context, dimension_scores, validation_errors or [])
    # Summary 子 Agent 启动
    result = await runtime.query_subagent(
        "summary",
        prompt,
        assembled_options=None,
        timeout_s=runtime_context.config.timeouts.summary_s,
    )
    report = _parse_summary_text(result.text)
    report["validation_errors"] = validation_errors or []
    artifact_path = runtime_context.result_dir / "summary_report.json"
    _write_json(artifact_path, report)
    _logger.info("ARTIFACT_WRITE path=%s", artifact_path)
    return report


def _build_summary_prompt(
    runtime_context: RuntimeContext,
    collected_context: dict[str, Any],
    dimension_scores: list[dict[str, Any]],
    validation_errors: list[str],
) -> str:
    skill_doc = load_skill_doc("summarize")
    summary_prompt = (_PROMPT_DIR / "summary.md").read_text(encoding="utf-8")
    summary_rule = (_PROMPT_DIR / "rules" / "summaryRule.md").read_text(encoding="utf-8")
    review_input = runtime_context.review_input
    payload = {
        "collected_context": _compact_context(collected_context),
        "dimension_scores": _compact_dimension_scores(dimension_scores),
        "raw_diff": _limit_text(review_input.diff_content, 8000),
        "title": review_input.title,
        "description": review_input.description,
        "commit_messages": review_input.commit_messages,
        "previous_report": review_input.previous_report,
        "requirements_doc": review_input.requirements_doc,
        "validation_errors": validation_errors,
    }
    return (
        f"{skill_doc}\n\n"
        "汇总提示词：\n"
        f"{summary_prompt}\n\n"
        "汇总评级规则：\n"
        f"{summary_rule}\n\n"
        "以下是本次运行输入：\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "请严格按照上方 SKILL.md、汇总提示词和汇总评级规则执行，并返回指定 JSON 输出。"
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


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _compact_context(context: dict[str, Any]) -> dict[str, Any]:
    keep = {
        "task_id",
        "artifact_path",
        "summary",
        "warnings",
        "changed_files",
        "diff_summary",
        "semantic_context",
        "call_graph_context",
        "code_snippets",
    }
    return {key: value for key, value in context.items() if key in keep}


def _compact_dimension_scores(scores: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in scores:
        findings = item.get("normalized_findings") or item.get("findings") or []
        compact.append(
            {
                "dimension": item.get("dimension"),
                "score": item.get("score"),
                "confidence": item.get("confidence"),
                "warnings": item.get("warnings", []),
                "artifact_path": item.get("artifact_path", ""),
                "findings": [_compact_finding(finding) for finding in findings if isinstance(finding, dict)],
            }
        )
    return compact


def _compact_finding(finding: dict[str, Any]) -> dict[str, Any]:
    return {
        "dimension": finding.get("dimension"),
        "title": finding.get("title"),
        "analysis": _limit_text(finding.get("analysis")),
        "evidence": _limit_text(finding.get("evidence")),
        "severity_hint": finding.get("severity_hint", ""),
        "file_path": finding.get("file_path", ""),
        "start_line": finding.get("start_line", 0),
        "end_line": finding.get("end_line", 0),
        "suggestion": _limit_text(finding.get("suggestion")),
        "score": finding.get("score", 0),
    }


def _limit_text(value: Any, limit: int = 1200) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"
