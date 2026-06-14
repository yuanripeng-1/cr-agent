from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from cr_agent.core.review_output import ReviewResult
from cr_agent.core.types import ValidationResult


async def validate_json(report: dict[str, Any]) -> ValidationResult:
    """
    纯代码 schema 校验。

    不调用 agent、不拿工具、不维护 attempt。attempt 仍只由主 agent 的
    summarize_report 工具 handler 计数。
    代码审查最终报告 report 是否符合 ReviewResult 的 JSON 契约——也就是后端要消费的 result.json 结构。

    """
    payload = _normalize_report(report)
    try:
        # ReviewResult 继承自 Pydantic 的 BaseModel
        # 所以这里可以使用 model_validate 方法来验证 payload 是否符合 ReviewResult 的 JSON 契约
        ReviewResult.model_validate(payload)
    except ValidationError as exc:
        return ValidationResult(valid=False, errors=_format_errors(exc))
    return ValidationResult(valid=True)


def _normalize_report(report: dict[str, Any]) -> dict[str, Any]:
    payload = dict(report)
    payload.setdefault("status", "success")
    payload.setdefault("log_path", "")
    payload.setdefault("tokens_consume", {})
    payload.setdefault("line_comments", {"comments": []})
    payload.setdefault("issues", [])
    return payload


def _format_errors(exc: ValidationError) -> list[str]:
    errors: list[str] = []
    for item in exc.errors():
        loc = ".".join(str(part) for part in item.get("loc", ())) or "<root>"
        msg = str(item.get("msg", "invalid"))
        errors.append(f"{loc}: {msg}")
    return errors
