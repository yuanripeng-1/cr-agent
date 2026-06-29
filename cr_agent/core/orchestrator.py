"""
orchestrator:审查运行的"幕后编排者"。

控制权归主 agent(它经 skill 工具自行规划 collect_context → dimension_review →
summarize_report,并在 validate 失败后决定是否重调)。orchestrator 不写顺序业务链,
只做四件支撑工作:
1. 构建 skill 工具与 max_retries 兜底(can_use_tool);
2. 启动主 agent 循环;
3. 汇总主/子 agent 的 token usage;
4. 组装 ReviewResult 并写盘(result.json / cr_result.md / run.log)。

主 agent 异常且尚无任何 summary 产物时,退化到 _run_sequential_skill_fallback
顺序跑同一套 skill,保证总能产出结构化结果。
"""

from __future__ import annotations

import json
import time
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
from cr_agent.core.review_timing import (
    log_review_timing,
    record_context_skill_s,
    record_summary_skill_s,
    start_review_timing,
)
from cr_agent.core.state import ReviewState
from cr_agent.core.types import TokenUsage
from cr_agent.core.usage import (
    accumulate_usage,
    accumulate_usage_from_dimension_artifacts,
    extract_usage,
    usage_breakdown_entry,
    usage_to_dict,
)
from cr_agent.skills.registry import SkillRegistry, build_default_skill_registry
from cr_agent.utils.logging import get_logger

_USER_PROMPT = "请使用可用的 skill 工具规划并运行代码评审。"
_MAIN_AGENT_PLANNING_TIMEOUT_S = 3000
_MAIN_AGENT_MAX_TURNS = 12
_logger = get_logger("cr_agent.core.orchestrator")


async def run_review(
    runtime_context: RuntimeContext,
    *,
    agent_runtime: MainAgentRuntime,
    skill_registry: SkillRegistry | None = None,
    max_retries: int = 2,
    main_timeout_s: float = 2000,
) -> ReviewResult:
    """
    主 agent 驱动的审查编排。

    agent_runtime 必填:控制流由主 agent 经 skill 工具发起,不再有纯 Python 顺序链回退。
    orchestrator 只负责构建工具、max_retries 兜底、usage 汇总与产物写盘。
    """
    if agent_runtime is None:
        raise ValueError("run_review requires a main agent runtime (agentic control flow)")

    # 创建 注册表：SkillRegistry，注册了 4 个 skill 函数（collect_context、dimension_review、summarize_report、validate_json）
    registry = skill_registry or build_default_skill_registry()

    # 创建 状态：ReviewState，本次审查运行的状态（记录成功/失败、重试次数、token 消耗）
    state = ReviewState(max_retries=max_retries)

    # 创建 会话：MainAgentSession，主 agent 运行期间共享的状态（收集的 context、维度评分、最终报告）
    session = MainAgentSession(
        runtime_context=runtime_context,
        registry=registry,
        state=state,
    )

    runtime_context.crg_lifecycle.start_background()
    append_run_log(runtime_context.result_dir, "review started")
    review_timing = start_review_timing()
    _logger.info("REVIEW_TIMING_START")
    try:
        # 把 3 个 skill 变成带 handler 的 ToolSpec 列表
        skill_tools = build_skill_tools(session)
        # summarize 超过重试次数就拒绝
        can_use_tool = make_backstop_can_use_tool(session)
        try:
            planning_timeout_s = min(main_timeout_s, _MAIN_AGENT_PLANNING_TIMEOUT_S)
            _logger.info(
                "MAIN_AGENT_PLANNING_START timeout_s=%s requested_timeout_s=%s",
                planning_timeout_s,
                main_timeout_s,
            )
            # 启动主 Agent 对话循环
            main_result = await agent_runtime.run_review_loop(
                system_prompt=MAIN_AGENT_SYSTEM_PROMPT,
                user_prompt=_USER_PROMPT,
                skill_tools=skill_tools,
                can_use_tool=can_use_tool,
                timeout_s=planning_timeout_s,
                max_turns=_MAIN_AGENT_MAX_TURNS,
            )
            main_usage = getattr(main_result, "usage", TokenUsage())
            state.tokens_consume = accumulate_usage(
                state.tokens_consume,
                main_usage,
            )
            state.token_usage_breakdown.append(
                usage_breakdown_entry(
                    stage="main_agent",
                    agent="main",
                    source="main_agent_runtime",
                    usage=main_usage,
                )
            )
        except Exception as exc:
            # 模型计费后才报错时,异常可能带回已消费 usage;累计后再抛,
            # 保证失败产物写真实已累计 token,不写假 0。
            partial = getattr(exc, "usage", None)
            if partial is not None:
                try:
                    partial_usage = extract_usage({"usage": partial})
                except Exception as usage_exc:
                    _logger.warning(
                        "USAGE_EXTRACT_FAILED source=main_agent_exception error=%s original_error=%s",
                        usage_exc,
                        exc,
                    )
                    partial_usage = TokenUsage()
                state.tokens_consume = accumulate_usage(
                    state.tokens_consume,
                    partial_usage,
                )
                state.token_usage_breakdown.append(
                    usage_breakdown_entry(
                        stage="main_agent",
                        agent="main",
                        source="main_agent_exception",
                        status="error",
                        usage=partial_usage,
                    )
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

    timing_summary = log_review_timing(review_timing)
    append_run_log(runtime_context.result_dir, timing_summary)

    write_token_usage_breakdown(runtime_context, state)
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
        skill_start = time.monotonic()
        collected = await session.registry.collect_context(session.runtime_context)
        record_context_skill_s(time.monotonic() - skill_start)
        _accumulate_result_usage(session, collected)
        _record_result_usage(
            session,
            collected,
            stage="collect_context",
            skill="collect_context",
            agent="context",
            artifact_path=str(collected.get("artifact_path", "")),
        )
        session.collected_context = collected
        _logger.info(
            "SKILL_END skill=collect_context source=fallback elapsed_s=%.2f",
            time.monotonic() - skill_start,
        )
    else:
        _logger.info("FALLBACK_SKIP_SKILL skill=collect_context reason=already_completed")

    if session.dimension_scores is None:
        _logger.info("MAIN_AGENT_SKILL_CALL skill=dimension_review source=fallback")
        _logger.info("SKILL_START skill=dimension_review source=fallback")
        if session.collected_context and session.collected_context.get("status") == "failed":
            error = str(session.collected_context.get("error") or "collect_context failed")
            session.state.errors.append(error)
            session.dimension_scores = []
            _logger.warning("SKILL_SKIP skill=dimension_review source=fallback reason=%s", error)
            return
            
        skill_start = time.monotonic()
        scores = await session.registry.dimension_review(
            session.runtime_context,
            session.collected_context or {},
        )
        accumulate_usage_from_dimension_artifacts(
            session.runtime_context.result_dir / "dimensions",
            add_usage=session.add_usage,
            add_breakdown=session.add_usage_breakdown,
        )
        session.dimension_scores = scores
        _logger.info(
            "SKILL_END skill=dimension_review source=fallback elapsed_s=%.2f",
            time.monotonic() - skill_start,
        )
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
        skill_start = time.monotonic()
        report = await session.registry.summarize_report(
            session.runtime_context,
            session.collected_context or {},
            session.dimension_scores or [],
            prior_errors,
        )
        # summary 子 agent 每次调用(含重试)都是真实计费,逐次累加其 usage。
        usage = report.get("usage")
        if isinstance(usage, dict):
            extracted = extract_usage({"usage": usage})
            session.add_usage(extracted)
            session.add_usage_breakdown(
                usage_breakdown_entry(
                    stage="summarize_report",
                    skill="summarize_report",
                    agent="summary",
                    attempt=session.state.attempt,
                    source="skill_result",
                    artifact_path=str(session.runtime_context.result_dir / "summary_report.json"),
                    usage=extracted,
                )
            )
        validation = await session.registry.validate_json(session.runtime_context, report)
        record_summary_skill_s(time.monotonic() - skill_start)
        session.last_report = report
        session.last_validation = validation
        _logger.info(
            "SKILL_END skill=summarize_report source=fallback valid=%s errors=%s elapsed_s=%.2f",
            validation.valid,
            len(validation.errors),
            time.monotonic() - skill_start,
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


def _record_result_usage(
    session: MainAgentSession,
    result: dict[str, Any],
    *,
    stage: str,
    skill: str | None = None,
    agent: str | None = None,
    artifact_path: str | None = None,
) -> None:
    usage = result.get("usage")
    if isinstance(usage, TokenUsage):
        extracted = usage
    elif isinstance(usage, dict):
        extracted = extract_usage({"usage": usage})
    else:
        return
    session.add_usage_breakdown(
        usage_breakdown_entry(
            stage=stage,
            skill=skill,
            agent=agent,
            source="skill_result",
            artifact_path=artifact_path,
            usage=extracted,
        )
    )


def write_token_usage_breakdown(
    runtime_context: RuntimeContext,
    state: ReviewState,
) -> None:
    payload = {
        "task_id": runtime_context.review_input.task_id,
        "platform": runtime_context.platform,
        "status": state.status,
        "attempts": state.attempt,
        "total": usage_to_dict(state.tokens_consume),
        "entries": state.token_usage_breakdown,
    }
    path = runtime_context.result_dir / "token_usage_breakdown.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    append_run_log(runtime_context.result_dir, f"token usage breakdown written: {path}")


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
        # input_tokens 为缓存拆分后的"非缓存输入";真实输入量需叠加 cache_creation/read。
        # cost 为各次模型调用 total_cost_usd 的累加(已含缓存读写折扣)。
        tokens_consume=OutputTokenUsage(
            input_tokens=state.tokens_consume.input_tokens,
            output_tokens=state.tokens_consume.output_tokens,
            cost=state.tokens_consume.cost,
            cache_creation_tokens=state.tokens_consume.cache_creation_tokens,
            cache_read_tokens=state.tokens_consume.cache_read_tokens,
        ),
        line_comments=report.get("line_comments") or LineComments(comments=[]),
        issues=report.get("issues") or [],
        platform=runtime_context.platform,
        errors=state.errors,
        warnings=state.warnings,
        attempts=state.attempt,
    )
