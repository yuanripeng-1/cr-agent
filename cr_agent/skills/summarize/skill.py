from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from cr_agent.bootstrap import RuntimeContext
from cr_agent.core.errors import RuntimeCallError, StructuredOutputError
from cr_agent.core.review_timing import record_summary_agent_s
from cr_agent.core.summary_output import SUMMARY_JSON_SCHEMA
from cr_agent.core.usage import usage_to_dict
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
    # 汇总仅依赖过滤后的维度缺陷，不注入 collected_context 完整 payload。
    del collected_context
    prompt = _build_summary_prompt(dimension_scores, validation_errors or [])
    prompt_path = runtime_context.result_dir / "summary_prompt.txt"
    prompt_path.write_text(prompt, encoding="utf-8")
    _logger.info("ARTIFACT_WRITE path=%s", prompt_path)
    return await invoke_summary_agent(
        runtime_context,
        prompt,
        validation_errors=validation_errors,
    )


async def invoke_summary_agent(
    runtime_context: RuntimeContext,
    prompt: str,
    validation_errors: list[str] | None = None,
    *,
    summary_report_txt_path: Path | None = None,
    summary_report_json_path: Path | None = None,
) -> dict[str, Any]:
    """
    向 summary subagent 发送 prompt 并落盘 summary_report 产物。

    与 summarize_report 共用同一套 structured / SDK 分支逻辑，供冒烟脚本复用。
    """
    runtime = getattr(runtime_context, "summary_runtime", None)
    if runtime is None:
        runtime = getattr(runtime_context, "runtime", None)
    if runtime is None:
        raise RuntimeCallError("summary runtime is not configured")

    raw_path = summary_report_txt_path or (
        runtime_context.result_dir / "summary_report.txt"
    )
    artifact_path = summary_report_json_path or (
        runtime_context.result_dir / "summary_report.json"
    )

    use_structured = bool(
        getattr(runtime_context.config.llm, "summary_structured_output", True)
    )
    _query_structured = getattr(runtime, "query_subagent_structured", None)
    structured_disabled = bool(getattr(runtime, "structured_output_disabled", False))
    if structured_disabled and getattr(runtime, "last_structured_output_error", None):
        _logger.warning(
            "SUMMARY_STRUCTURED_OUTPUT_SKIPPED reason=disabled detail=%s",
            runtime.last_structured_output_error,
        )

    if use_structured and _query_structured is not None and not structured_disabled:
        _logger.info("SUMMARY_CALL_MODE mode=structured_output")
        agent_start = time.monotonic()
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
            _write_structured_failure_artifacts(
                runtime_context,
                exc,
                error_report,
                raw_path=raw_path,
                artifact_path=artifact_path,
            )
            return error_report
        agent_elapsed = time.monotonic() - agent_start
        record_summary_agent_s(agent_elapsed)
        _logger.info("AGENT_TIMING agent=summary elapsed_s=%.2f", agent_elapsed)
    else:
        if not use_structured:
            mode = "query_subagent_no_structured_config"
        elif structured_disabled:
            mode = "query_subagent_no_structured"
        else:
            mode = "query_subagent"
        _logger.info("SUMMARY_CALL_MODE mode=%s", mode)
        agent_start = time.monotonic()
        result = await runtime.query_subagent(
            "summary",
            prompt,
            assembled_options=None,
            timeout_s=runtime_context.config.timeouts.summary_s,
        )
        agent_elapsed = time.monotonic() - agent_start
        record_summary_agent_s(agent_elapsed)
        _logger.info("AGENT_TIMING agent=summary elapsed_s=%.2f", agent_elapsed)

    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(result.text, encoding="utf-8")
    _logger.info("ARTIFACT_WRITE path=%s", raw_path)

    report = _parse_summary_text(result.text)
    report["validation_errors"] = validation_errors or []
    report["usage"] = usage_to_dict(result.usage)
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
    *,
    raw_path: Path | None = None,
    artifact_path: Path | None = None,
) -> None:
    """落盘 structured 失败诊断，避免把 SDK fallback 乱码写入 summary_report。"""
    raw_path = raw_path or (runtime_context.result_dir / "summary_report.txt")
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(
        f"[SUMMARY_STRUCTURED_OUTPUT_FAILED kind={exc.kind}]\n{exc}\n",
        encoding="utf-8",
    )
    _logger.info("ARTIFACT_WRITE path=%s reason=structured_output_failed", raw_path)
    artifact_path = artifact_path or (runtime_context.result_dir / "summary_report.json")
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
