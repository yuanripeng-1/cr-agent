from __future__ import annotations

from typing import Any

from cr_agent.bootstrap import RuntimeContext
from cr_agent.core.artifacts import (
    append_run_log,
    write_result_json,
    write_result_markdown,
)
from cr_agent.core.main_agent import (
    MAIN_AGENT_SYSTEM_PROMPT,
    MainAgentRuntime,
    MainAgentSession,
    build_skill_tools,
    make_backstop_can_use_tool,
)
from cr_agent.core.review_output import (
    LineComments,
    ReviewResult,
    TokenUsage as OutputTokenUsage,
)
from cr_agent.core.state import ReviewState
from cr_agent.core.types import TokenUsage
from cr_agent.core.usage import accumulate_usage, extract_usage
from cr_agent.skills.registry import SkillRegistry, build_default_skill_registry

_USER_PROMPT = "Plan and run the code review using the available skill tools."


async def run_review(
    runtime_context: RuntimeContext,
    *,
    agent_runtime: MainAgentRuntime,
    skill_registry: SkillRegistry | None = None,
    max_retries: int = 2,
) -> ReviewResult:
    """
    主 agent 驱动的审查编排。

    agent_runtime 必填:控制流由主 agent 经 skill 工具发起,不再有纯 Python 顺序链回退。
    orchestrator 只负责构建工具、max_retries 兜底、usage 汇总与产物写盘。
    """
    if agent_runtime is None:
        raise ValueError("run_review requires a main agent runtime (agentic control flow)")

    registry = skill_registry or build_default_skill_registry()
    state = ReviewState(max_retries=max_retries)
    session = MainAgentSession(
        runtime_context=runtime_context,
        registry=registry,
        state=state,
    )

    append_run_log(runtime_context.result_dir, "review started")
    try:
        skill_tools = build_skill_tools(session)
        can_use_tool = make_backstop_can_use_tool(session)
        try:
            main_result = await agent_runtime.run_review_loop(
                system_prompt=MAIN_AGENT_SYSTEM_PROMPT,
                user_prompt=_USER_PROMPT,
                skill_tools=skill_tools,
                can_use_tool=can_use_tool,
            )
            state.tokens_consume = accumulate_usage(
                state.tokens_consume,
                getattr(main_result, "usage", TokenUsage()),
            )
        except Exception as exc:
            # 模型计费后才报错时,异常可能带回已消费 usage;累计后再抛,
            # 保证失败产物写真实已累计 token,不写假 0。
            partial = getattr(exc, "usage", None)
            if partial is not None:
                state.tokens_consume = accumulate_usage(
                    state.tokens_consume,
                    extract_usage({"usage": partial}),
                )
            raise

        validation = session.last_validation
        if session.last_report is not None and validation is not None and validation.valid:
            state.status = "success"
        else:
            state.status = "failed"
            if validation is not None:
                state.errors.extend(validation.errors)
            else:
                state.errors.append("summary not generated")

        result = _build_review_result(
            runtime_context=runtime_context,
            state=state,
            report=session.last_report or {},
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
