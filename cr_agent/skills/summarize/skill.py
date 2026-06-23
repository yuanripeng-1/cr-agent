from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cr_agent.bootstrap import RuntimeContext
from cr_agent.core.errors import RuntimeCallError, StructuredOutputError
from cr_agent.core.summary_output import SUMMARY_JSON_SCHEMA
from cr_agent.core.types import TokenUsage
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

    # 汇总仅依赖过滤后的维度缺陷，不注入 collected_context 完整 payload。
    del collected_context
    prompt = _build_summary_prompt(dimension_scores, validation_errors or [])
    prompt_path = runtime_context.result_dir / "summary_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    _logger.info("ARTIFACT_WRITE path=%s", prompt_path)
    # 优先使用 query_subagent_structured（LiteLLM structured output）；
    # 若同次 review 内 structured 已失败，则改走 query_subagent（不再 structured）。
    # runtime 若为测试 fake（无 query_subagent_structured 方法），自动退化为 query_subagent。
    _query_structured = getattr(runtime, "query_subagent_structured", None)
    structured_disabled = bool(getattr(runtime, "structured_output_disabled", False))
    if structured_disabled and getattr(runtime, "last_structured_output_error", None):
        _logger.warning(
            "SUMMARY_STRUCTURED_OUTPUT_SKIPPED reason=disabled detail=%s",
            runtime.last_structured_output_error,
        )

    if _query_structured is not None and not structured_disabled:
        _logger.info("SUMMARY_CALL_MODE mode=structured_output")
        try:
            result = await _query_structured(
                "summary",
                prompt,
                response_schema=SUMMARY_JSON_SCHEMA,
                timeout_s=runtime_context.config.timeouts.summary_s,
            )
        except StructuredOutputError as exc:
            _logger.error(
                "SUMMARY_STRUCTURED_OUTPUT_FAILED kind=%s error=%s",
                exc.kind,
                exc,
            )
            error_report = _structured_output_failure_report(validation_errors, exc)
            _write_structured_failure_artifacts(runtime_context, exc, error_report)
            return error_report
    else:
        mode = "query_subagent"
        if structured_disabled:
            mode = "query_subagent_no_structured"
        _logger.info("SUMMARY_CALL_MODE mode=%s", mode)
        result = await runtime.query_subagent(
            "summary",
            prompt,
            assembled_options=None,
            timeout_s=runtime_context.config.timeouts.summary_s,
        )
    # 先把模型原始输出落盘，供调试查看真实响应（解析前）
    raw_path = runtime_context.result_dir / "summary_report.txt"
    raw_path.write_text(result.text, encoding="utf-8")
    _logger.info("ARTIFACT_WRITE path=%s", raw_path)

    report = _parse_summary_text(result.text)
    report["validation_errors"] = validation_errors or []
    # summary 子 agent 是独立模型调用,其 usage 不在主 agent 循环内;
    # 带回 report 供上层逐次累加,否则这部分 token 会被完全漏统计。
    report["usage"] = _usage_dict(result.usage)
    artifact_path = runtime_context.result_dir / "summary_report.json"
    _write_json(artifact_path, report)
    _logger.info("ARTIFACT_WRITE path=%s", artifact_path)
    return report


def _structured_output_failure_report(
    validation_errors: list[str] | None,
    exc: StructuredOutputError,
) -> dict[str, Any]:
    """structured output 失败时返回必失败的占位报告，触发 validate + 主 agent 重试。"""
    prior = list(validation_errors or [])
    prior.append(f"structured_output[{exc.kind}]: {exc}")
    return {
        "llm_result": "",
        "line_comments": {"comments": []},
        "issues": [],
        "structured_output_error": str(exc),
        "structured_output_error_kind": exc.kind,
        "validation_errors": prior,
    }


def _write_structured_failure_artifacts(
    runtime_context: RuntimeContext,
    exc: StructuredOutputError,
    error_report: dict[str, Any],
) -> None:
    """落盘 structured 失败诊断，避免把 SDK fallback 乱码写入 summary_report。"""
    raw_path = runtime_context.result_dir / "summary_report.txt"
    raw_path.write_text(
        f"[SUMMARY_STRUCTURED_OUTPUT_FAILED kind={exc.kind}]\n{exc}\n",
        encoding="utf-8",
    )
    _logger.info("ARTIFACT_WRITE path=%s reason=structured_output_failed", raw_path)
    artifact_path = runtime_context.result_dir / "summary_report.json"
    _write_json(artifact_path, error_report)
    _logger.info("ARTIFACT_WRITE path=%s reason=structured_output_failed", artifact_path)


def _build_summary_prompt(
    dimension_scores: list[dict[str, Any]],
    validation_errors: list[str],
) -> str:
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
        "汇总提示词：\n"
        f"{summary_prompt}\n\n"
        "汇总评级规则：\n"
        f"{summary_rule}\n\n"
        "请对各维度评审 findings 按评级规则定级，并输出 JSON，字段为："
        "llm_result、line_comments、issues。运行时字段由 Python 补充。\n"
        f"{validation_note}"
        "各维度评审 findings：\n"
        f"{json.dumps(filtered_reports, ensure_ascii=False, indent=2)}\n"
        "最终要输出的内容必须按照<output>...</output>内的内容进行输出（不要返回标签），并且只需要输出这个一个good output"
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


def _usage_dict(usage: TokenUsage) -> dict[str, Any]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cost": usage.cost,
    }


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
