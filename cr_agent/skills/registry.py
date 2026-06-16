from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from cr_agent.bootstrap import RuntimeContext
from cr_agent.core.types import ValidationResult
from cr_agent.skills.collect_context.skill import collect_context
from cr_agent.skills.dimension_review.skill import dimension_review
from cr_agent.skills.summarize.skill import summarize_report
from cr_agent.skills.validate_json.skill import validate_json


CollectContextFn = Callable[[RuntimeContext], Awaitable[dict[str, Any]]]
DimensionReviewFn = Callable[[RuntimeContext, dict[str, Any]], Awaitable[list[dict[str, Any]]]]
SummarizeReportFn = Callable[
    [RuntimeContext, dict[str, Any], list[dict[str, Any]], list[str] | None],
    Awaitable[dict[str, Any]],
]
ValidateJsonFn = Callable[[RuntimeContext, dict[str, Any]], Awaitable[ValidationResult]]


@dataclass(frozen=True)
class SkillRegistry:
    collect_context: CollectContextFn
    dimension_review: DimensionReviewFn
    summarize_report: SummarizeReportFn
    validate_json: ValidateJsonFn


def build_default_skill_registry() -> SkillRegistry:
    return SkillRegistry(
        collect_context=collect_context,
        dimension_review=dimension_review,
        summarize_report=summarize_report,
        validate_json=validate_json,
    )


def registered_skill_names() -> set[str]:
    return {
        "collect_context",
        "dimension_review",
        "summarize_report",
        "validate_json",
    }
