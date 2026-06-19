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
    prompt_path = runtime_context.result_dir / "summary_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    _logger.info("ARTIFACT_WRITE path=%s", prompt_path)
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
    del runtime_context, collected_context  # 汇总上下文仅使用过滤后的维度缺陷，不注入完整 payload。
    skill_doc = load_skill_doc("summarize")
    summary_prompt = (_PROMPT_DIR / "summary.md").read_text(encoding="utf-8")
    summary_rule = (_PROMPT_DIR / "rules" / "summaryRule.md").read_text(encoding="utf-8")
    filtered_reports = _compact_findings(dimension_scores)
    validation_note = ""
    if validation_errors:
        validation_note = (
            "\n上次输出未通过 schema 校验，请只修复下列错误，不要扩大评审范围：\n"
            f"{json.dumps(validation_errors, ensure_ascii=False, indent=2)}\n"
        )
    return (
        f"{skill_doc}\n\n"
        "汇总提示词：\n"
        f"{summary_prompt}\n\n"
        "汇总评级规则：\n"
        f"{summary_rule}\n\n"
        "请生成最终代码评审内容，格式为 JSON，字段为："
        "llm_result、line_comments、issues。运行时字段由 Python 补充。\n"
        f"{validation_note}"
        "结果过滤的评分后缺陷总结文件：\n"
        f"{json.dumps(filtered_reports, ensure_ascii=False, indent=2)}"
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


def _compact_findings(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        nested = item.get("findings")
        if isinstance(nested, list):
            for finding in nested:
                if isinstance(finding, dict):
                    compact.append(_compact_finding(finding))
            continue
        compact.append(_compact_finding(item))
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
