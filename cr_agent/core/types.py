from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TokenUsage:
    # 注意:启用提示词缓存后,input_tokens 仅表示"未命中缓存且未用于建缓存"的全新输入;
    # 命中缓存的输入计入 cache_read_tokens,写入缓存的计入 cache_creation_tokens。
    # 真实输入量 ≈ input_tokens + cache_creation_tokens + cache_read_tokens。
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    # 美元成本:来自 SDK ResultMessage.total_cost_usd(已含缓存读写折扣);无则 0.0。
    cost: float = 0.0


@dataclass(frozen=True)
class QueryResult:
    text: str
    usage: TokenUsage = field(default_factory=TokenUsage)
    raw: Any | None = None
    error: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    errors: list[str] = field(default_factory=list)

