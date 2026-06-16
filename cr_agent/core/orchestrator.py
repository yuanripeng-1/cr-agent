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
from cr_agent.utils.logging import get_logger

_USER_PROMPT = "Plan and run the code review using the available skill tools."
_MAIN_AGENT_PLANNING_TIMEOUT_S = 600
_MAIN_AGENT_MAX_TURNS = 12
_logger = get_logger("cr_agent.core.orchestrator")


async def run_review(
    runtime_context: RuntimeContext,
    *,
    agent_runtime: MainAgentRuntime,
    skill_registry: SkillRegistry | None = None,
    max_retries: int = 2,
    main_timeout_s: float = 300,
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

    runtime_context.crg_lifecycle.start_background()
    append_run_log(runtime_context.result_dir, "review started")
    try:
        skill_tools = build_skill_tools(session)
        can_use_tool = make_backstop_can_use_tool(session)
        try:
            planning_timeout_s = min(main_timeout_s, _MAIN_AGENT_PLANNING_TIMEOUT_S)
            _logger.info(
                "MAIN_AGENT_PLANNING_START timeout_s=%s requested_timeout_s=%s",
                planning_timeout_s,
                main_timeout_s,
            )
            main_result = await agent_runtime.run_review_loop(
                system_prompt=MAIN_AGENT_SYSTEM_PROMPT,
                user_prompt=_USER_PROMPT,
                skill_tools=skill_tools,
                can_use_tool=can_use_tool,
                timeout_s=planning_timeout_s,
                max_turns=_MAIN_AGENT_MAX_TURNS,
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
            if session.last_report is None:
                _logger.warning(
                    "DEGRADED reason=MAIN_AGENT_FAILED_RECOVERING error=%s",
                    exc,
                )
                state.warnings.append(f"main agent fallback: {exc}")
                await _run_sequential_skill_fallback(
                    session=session,
                    max_retries=max_retries,
                )
            else:
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


async def _run_sequential_skill_fallback(
    *,
    session: MainAgentSession,
    max_retries: int,
) -> None:
    """
    主 agent/SDK 未能稳定触发工具时的恢复路径。

    正常控制权仍归主 agent;这里仅在主 agent 抛错且还没有任何 summary 产物时,
    按同一套 skill registry 顺序执行,保证真实审查流程能产出 result.json。
    """
    _logger.info("FALLBACK_SKILL_FLOW_START reason=main_agent_failed")
    if session.collected_context is None:
        _logger.info("MAIN_AGENT_SKILL_CALL skill=collect_context source=fallback")
        _logger.info("SKILL_START skill=collect_context source=fallback")
        collected = await session.registry.collect_context(session.runtime_context)
        _accumulate_result_usage(session, collected)
        session.collected_context = collected
        _logger.info("SKILL_END skill=collect_context source=fallback")
    else:
        _logger.info("FALLBACK_SKIP_SKILL skill=collect_context reason=already_completed")

    if session.dimension_scores is None:
        _logger.info("MAIN_AGENT_SKILL_CALL skill=dimension_review source=fallback")
        _logger.info("SKILL_START skill=dimension_review source=fallback")
        scores = await session.registry.dimension_review(
            session.runtime_context,
            session.collected_context or {},
        )
        for item in scores:
            _accumulate_result_usage(session, item)
        session.dimension_scores = scores
        _logger.info("SKILL_END skill=dimension_review source=fallback")
    else:
        _logger.info("FALLBACK_SKIP_SKILL skill=dimension_review reason=already_completed")

    for _ in range(max_retries + 1):
        _logger.info(
            "MAIN_AGENT_SKILL_CALL skill=summarize_report source=fallback attempt_next=%s",
            session.state.attempt + 1,
        )
        session.state.attempt += 1
        prior_errors = (
            session.last_validation.errors if session.last_validation is not None else None
        )
        _logger.info(
            "SKILL_START skill=summarize_report source=fallback attempt=%s",
            session.state.attempt,
        )
        report = await session.registry.summarize_report(
            session.runtime_context,
            session.collected_context or {},
            session.dimension_scores or [],
            prior_errors,
        )
        validation = await session.registry.validate_json(session.runtime_context, report)
        session.last_report = report
        session.last_validation = validation
        _logger.info(
            "SKILL_END skill=summarize_report source=fallback valid=%s errors=%s",
            validation.valid,
            len(validation.errors),
        )
        if validation.valid:
            break
    _logger.info(
        "FALLBACK_SKILL_FLOW_END valid=%s attempts=%s",
        session.last_validation.valid if session.last_validation is not None else None,
        session.state.attempt,
    )


def _accumulate_result_usage(session: MainAgentSession, result: dict[str, Any]) -> None:
    usage = result.get("usage")
    if isinstance(usage, TokenUsage):
        session.add_usage(usage)
    elif isinstance(usage, dict):
        session.add_usage(extract_usage({"usage": usage}))


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
        line_comments=report.get("line_comments") or LineComments(comments=[]),
        issues=report.get("issues") or [],
        task_id=runtime_context.review_input.task_id,
        platform=runtime_context.platform,
        errors=state.errors,
        warnings=state.warnings,
        attempts=state.attempt,
    )
