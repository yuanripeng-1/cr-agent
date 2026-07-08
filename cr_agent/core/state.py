from __future__ import annotations

from dataclasses import dataclass, field

from cr_agent.core.types import TokenUsage


@dataclass
class ReviewState:
    status: str = "running"
    attempt: int = 0
    max_retries: int = 2
    tokens_consume: TokenUsage = field(default_factory=TokenUsage)
    token_usage_breakdown: list[dict] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
