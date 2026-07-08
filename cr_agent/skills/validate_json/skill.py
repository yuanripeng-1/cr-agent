from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from cr_agent.bootstrap import RuntimeContext
from cr_agent.core.review_output import ReviewResult
from cr_agent.core.summary_output import validate_summary_output
from cr_agent.core.types import ValidationResult
from cr_agent.utils.logging import get_logger

_logger = get_logger("cr_agent.skills.validate_json")


async def validate_json(runtime_context: RuntimeContext, report: dict[str, Any]) -> ValidationResult:
    """
    纯代码 schema 三阶段校验。

    第一阶段：SummaryOutput 严格校验——检查 LLM 必须输出的 3 个字段是否完整存在，
              对原始 report（未补充运行时字段）进行校验。
    第二阶段：ReviewResult 契约校验——补充运行时字段后校验后端消费的完整契约。
    第三阶段：语义校验——comments 与 issues.locations 数量对齐。

    不调用 agent、不拿工具、不维护 attempt。
    每阶段结果均写入 run.log，便于排查。
    """
    errors: list[str] = []

    # 第一阶段：SummaryOutput 严格字段校验（LLM 直接输出，不含运行时补充字段）
    _logger.info("SUMMARY_OUTPUT_VALIDATE_START")
    summary_errors = validate_summary_output(report)
    if summary_errors:
        _logger.warning(
            "SUMMARY_OUTPUT_VALIDATE_FAILED error_count=%d errors=%s",
            len(summary_errors),
            summary_errors,
        )
        errors.extend(summary_errors)
    else:
        _logger.info("SUMMARY_OUTPUT_VALIDATE_PASSED")

    # 第二阶段：ReviewResult 契约校验（补充 status / log_path / tokens_consume 等运行时字段后）
    payload = _normalize_report(runtime_context, report)
    try:
        ReviewResult.model_validate(payload)
    except ValidationError as exc:
        review_errors = _format_errors(exc)
        if review_errors:
            _logger.warning(
                "REVIEW_RESULT_VALIDATE_FAILED error_count=%d errors=%s",
                len(review_errors),
                review_errors,
            )
            errors.extend(review_errors)

    # 第三阶段：语义校验（comments 与 issues.locations 数量对齐）
    semantic_errors = _semantic_errors(payload)
    if semantic_errors:
        _logger.warning(
            "SEMANTIC_VALIDATE_FAILED error_count=%d errors=%s",
            len(semantic_errors),
            semantic_errors,
        )
        errors.extend(semantic_errors)

    valid = not errors
    if valid:
        _logger.info("VALIDATE_JSON_PASSED")
    else:
        _logger.warning("VALIDATE_JSON_FAILED total_error_count=%d", len(errors))
    return ValidationResult(valid=valid, errors=errors)


def _normalize_report(runtime_context: RuntimeContext, report: dict[str, Any]) -> dict[str, Any]:
    payload = dict(report)
    payload.setdefault("status", "success")
    payload.setdefault("log_path", str(runtime_context.result_dir / "run.log"))
    payload.setdefault("tokens_consume", {})
    payload.setdefault("line_comments", {"comments": []})
    payload.setdefault("issues", [])
    payload.setdefault("task_id", runtime_context.review_input.task_id)
    payload.setdefault("platform", runtime_context.platform)
    return payload


def _format_errors(exc: ValidationError) -> list[str]:
    errors: list[str] = []
    for item in exc.errors():
        loc = ".".join(str(part) for part in item.get("loc", ())) or "<root>"
        msg = str(item.get("msg", "invalid"))
        errors.append(f"{loc}: {msg}")
    return errors


def _semantic_errors(payload: dict[str, Any]) -> list[str]:
    """语义校验：comments 与 issues.locations 的数量对齐检查。"""
    errors: list[str] = []
    comments = payload.get("line_comments", {}).get("comments", [])
    issues = payload.get("issues", [])
    locations = [
        location
        for issue in issues
        if isinstance(issue, dict)
        for location in issue.get("locations", [])
        if isinstance(location, dict)
    ]
    if len(comments) != len(locations):
        errors.append(
            "line_comments.comments: count must equal total issues.locations count"
        )
    return errors
