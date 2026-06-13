from __future__ import annotations

from typing import Any, Protocol

from cr_agent.bootstrap import RuntimeContext
from cr_agent.core.artifacts import (
    append_run_log,
    write_result_json,
    write_result_markdown,
)
from cr_agent.core.review_output import (
    LineComments,
    ReviewResult,
    TokenUsage as OutputTokenUsage,
)
from cr_agent.core.state import ReviewState
from cr_agent.core.types import TokenUsage, ValidationResult
from cr_agent.core.usage import accumulate_usage
from cr_agent.skills.registry import SkillRegistry, build_default_skill_registry


class AgentRuntime(Protocol):
    async def query_main(self, prompt: str, **kwargs: Any) -> Any:
        ...


async def run_review(
    runtime_context: RuntimeContext,
    *,
    agent_runtime: AgentRuntime | None = None,
    skill_registry: SkillRegistry | None = None,
    max_retries: int = 2,
) -> ReviewResult:
    registry = skill_registry or build_default_skill_registry()
    state = ReviewState(max_retries=max_retries)

    append_run_log(runtime_context.result_dir, "review started")
    try:
        if agent_runtime is not None:
            main_result = await agent_runtime.query_main(
                "Plan and run the code review using the available skills."
            )
            state.tokens_consume = accumulate_usage(
                state.tokens_consume,
                getattr(main_result, "usage", TokenUsage()),
            )

        collected_context = await registry.collect_context(runtime_context)
        dimension_scores = await registry.dimension_review(collected_context)

        report: dict[str, Any] | None = None
        validation = ValidationResult(valid=False, errors=["summary not generated"])
        while state.attempt <= state.max_retries:
            report = await registry.summarize_report(
                collected_context,
                dimension_scores,
                validation.errors,
            )
            validation = await registry.validate_json(report)
            if validation.valid:
                state.status = "success"
                break
            state.attempt += 1

        if state.status != "success":
            state.status = "failed"
            state.errors.extend(validation.errors)

        result = _build_review_result(
            runtime_context=runtime_context,
            state=state,
            report=report or {},
        )
    except Exception as exc:
        state.status = "error"
        state.errors.append(str(exc))
        result = _build_review_result(
            runtime_context=runtime_context,
            state=state,
            report={"llm_result": "# CR-Agent\n\nReview failed before completion."},
        )
        append_run_log(runtime_context.result_dir, f"review error: {exc}")

    write_result_json(runtime_context.result_dir, result)
    write_result_markdown(runtime_context.result_dir, result.llm_result)
    append_run_log(runtime_context.result_dir, f"review finished status={result.status}")
    return result


def _build_review_result(
    *,
    runtime_context: RuntimeContext,
    state: ReviewState,
    report: dict[str, Any],
) -> ReviewResult:
    llm_result = str(report.get("llm_result") or report.get("summary") or "# CR-Agent\n")
    status = state.status if state.status in {"success", "failed", "error"} else "error"
    return ReviewResult(
        status=status,
        llm_result=llm_result,
        log_path=str(runtime_context.result_dir / "run.log"),
        tokens_consume=OutputTokenUsage(
            input_tokens=state.tokens_consume.input_tokens,
            output_tokens=state.tokens_consume.output_tokens,
            cost=0.0,
            cache_creation_tokens=state.tokens_consume.cache_creation_tokens,
            cache_read_tokens=state.tokens_consume.cache_read_tokens,
        ),
        line_comments=LineComments(comments=[]),
        issues=[],
        task_id=runtime_context.review_input.task_id,
        platform=runtime_context.platform,
        errors=state.errors,
        warnings=state.warnings,
        attempts=state.attempt,
    )

