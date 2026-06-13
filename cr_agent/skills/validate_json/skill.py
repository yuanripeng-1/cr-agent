from __future__ import annotations

from typing import Any

from cr_agent.core.types import ValidationResult


async def validate_json(report: dict[str, Any]) -> ValidationResult:
    if str(report.get("llm_result", "")).strip():
        return ValidationResult(valid=True)
    return ValidationResult(valid=False, errors=["llm_result is required"])
