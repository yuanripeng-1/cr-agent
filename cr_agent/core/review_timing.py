"""Review 运行耗时汇总：经 contextvar 在 skill / subagent 间共享,结束时写入 run.log。"""

from __future__ import annotations

import contextvars
import time
from dataclasses import dataclass, field

from cr_agent.utils.logging import get_logger

_logger = get_logger("cr_agent.core.review_timing")

_review_timing: contextvars.ContextVar[ReviewTiming | None] = contextvars.ContextVar(
    "review_timing",
    default=None,
)


@dataclass
class ReviewTiming:
    started_at: float = field(default_factory=time.monotonic)
    context_s: float = 0.0
    context_skill_s: float = 0.0
    dimension_s: dict[str, float] = field(default_factory=dict)
    dimension_skill_s: float = 0.0
    summary_attempts_s: list[float] = field(default_factory=list)
    summary_skill_s: list[float] = field(default_factory=list)
    main_s: float = 0.0

    def total_s(self) -> float:
        return time.monotonic() - self.started_at

    def dimension_total_s(self) -> float:
        return sum(self.dimension_s.values())

    def summary_total_s(self) -> float:
        return sum(self.summary_attempts_s)

    def summary_skill_total_s(self) -> float:
        return sum(self.summary_skill_s)


def start_review_timing() -> ReviewTiming:
    timing = ReviewTiming()
    _review_timing.set(timing)
    return timing


def current_review_timing() -> ReviewTiming | None:
    return _review_timing.get()


def record_context_agent_s(elapsed_s: float) -> None:
    timing = current_review_timing()
    if timing is not None:
        timing.context_s = elapsed_s


def record_context_skill_s(elapsed_s: float) -> None:
    timing = current_review_timing()
    if timing is not None:
        timing.context_skill_s = elapsed_s


def record_dimension_agent_s(dimension: str, elapsed_s: float) -> None:
    timing = current_review_timing()
    if timing is not None:
        timing.dimension_s[dimension] = elapsed_s


def record_dimension_skill_s(elapsed_s: float) -> None:
    timing = current_review_timing()
    if timing is not None:
        timing.dimension_skill_s = elapsed_s


def record_summary_agent_s(elapsed_s: float) -> None:
    timing = current_review_timing()
    if timing is not None:
        timing.summary_attempts_s.append(elapsed_s)


def record_summary_skill_s(elapsed_s: float) -> None:
    timing = current_review_timing()
    if timing is not None:
        timing.summary_skill_s.append(elapsed_s)


def record_main_s(elapsed_s: float) -> None:
    timing = current_review_timing()
    if timing is not None:
        timing.main_s = elapsed_s


def format_review_timing_summary(timing: ReviewTiming) -> str:
    return (
        "REVIEW_TIMING "
        f"total_s={timing.total_s():.2f} "
        f"context_s={timing.context_s:.2f} "
        f"context_skill_s={timing.context_skill_s:.2f} "
        f"dimension_total_s={timing.dimension_total_s():.2f} "
        f"dimension_skill_s={timing.dimension_skill_s:.2f} "
        f"summary_total_s={timing.summary_total_s():.2f} "
        f"summary_skill_total_s={timing.summary_skill_total_s():.2f} "
        f"summary_attempts={len(timing.summary_attempts_s)} "
        f"main_s={timing.main_s:.2f}"
    )


def log_review_timing(timing: ReviewTiming) -> str:
    summary = format_review_timing_summary(timing)
    _logger.info(summary)
    for dimension, elapsed_s in sorted(timing.dimension_s.items()):
        _logger.info("REVIEW_TIMING dimension=%s elapsed_s=%.2f", dimension, elapsed_s)
    for attempt, elapsed_s in enumerate(timing.summary_attempts_s, 1):
        _logger.info(
            "REVIEW_TIMING summary attempt=%s agent_elapsed_s=%.2f",
            attempt,
            elapsed_s,
        )
    for attempt, elapsed_s in enumerate(timing.summary_skill_s, 1):
        _logger.info(
            "REVIEW_TIMING summary attempt=%s skill_elapsed_s=%.2f",
            attempt,
            elapsed_s,
        )
    return summary
