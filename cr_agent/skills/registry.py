"""
skill 注册表:把 4 个 skill 函数(collect_context / dimension_review /
summarize_report / validate_json)聚合成一个可注入的 SkillRegistry。

skill = 审查流程中的一个可复用阶段能力;主 agent 经 build_skill_tools 把其中
前 3 个包装成可调用工具,validate_json 作为纯代码校验由 summarize 流程内部调用。
单测可构造自定义 registry 注入 fake skill。
"""

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
