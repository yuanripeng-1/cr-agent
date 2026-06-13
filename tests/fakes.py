from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cr_agent.core.types import QueryResult, TokenUsage, ValidationResult
from cr_agent.skills.registry import SkillRegistry


@dataclass
class FakeAgentRuntime:
    results: list[QueryResult] = field(default_factory=list)
    calls: list[str] = field(default_factory=list)
    error: Exception | None = None

    async def query_main(self, prompt: str, **kwargs: Any) -> QueryResult:
        self.calls.append(prompt)
        if self.error is not None:
            raise self.error
        if self.results:
            return self.results.pop(0)
        return QueryResult(text="fake main completed", usage=TokenUsage())


class FakeSkillScenario:
    def __init__(self, *, validation_failures: int = 0) -> None:
        self.validation_failures = validation_failures
        self.summarize_calls = 0
        self.validate_calls = 0

    def registry(self) -> SkillRegistry:
        return SkillRegistry(
            collect_context=self.collect_context,
            dimension_review=self.dimension_review,
            summarize_report=self.summarize_report,
            validate_json=self.validate_json,
        )

    async def collect_context(self, runtime_context: Any) -> dict[str, Any]:
        return {
            "task_id": runtime_context.review_input.task_id,
            "platform": runtime_context.platform,
        }

    async def dimension_review(self, collected_context: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"dimension": "fake", "score": 100, "findings": []}]

    async def summarize_report(
        self,
        collected_context: dict[str, Any],
        dimension_scores: list[dict[str, Any]],
        validation_errors: list[str] | None = None,
    ) -> dict[str, Any]:
        self.summarize_calls += 1
        return {
            "llm_result": (
                "# fake report\n\n"
                f"attempt={self.summarize_calls} "
                f"errors={len(validation_errors or [])}"
            )
        }

    async def validate_json(self, report: dict[str, Any]) -> ValidationResult:
        self.validate_calls += 1
        if self.validate_calls <= self.validation_failures:
            return ValidationResult(valid=False, errors=["invalid fake report"])
        return ValidationResult(valid=True)

