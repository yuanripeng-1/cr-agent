from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from cr_agent.core.types import TokenUsage


def accumulate_usage(total: TokenUsage, call: TokenUsage) -> TokenUsage:
    return TokenUsage(
        input_tokens=total.input_tokens + call.input_tokens,
        output_tokens=total.output_tokens + call.output_tokens,
        cache_creation_tokens=(
            total.cache_creation_tokens + call.cache_creation_tokens
        ),
        cache_read_tokens=total.cache_read_tokens + call.cache_read_tokens,
    )


def extract_usage(raw_response: Any) -> TokenUsage:
    usage = _usage_payload(raw_response)
    if usage is None:
        return TokenUsage()

    return TokenUsage(
        input_tokens=_int_field(usage, "input_tokens"),
        output_tokens=_int_field(usage, "output_tokens"),
        cache_creation_tokens=_int_field(
            usage,
            "cache_creation_input_tokens",
            "cache_creation_tokens",
        ),
        cache_read_tokens=_int_field(
            usage,
            "cache_read_input_tokens",
            "cache_read_tokens",
        ),
    )


def _usage_payload(raw_response: Any) -> Any | None:
    if raw_response is None:
        return None
    if isinstance(raw_response, Mapping):
        return raw_response.get("usage")
    return getattr(raw_response, "usage", None)


def _int_field(payload: Any, *names: str) -> int:
    for name in names:
        if isinstance(payload, Mapping):
            value = payload.get(name)
        else:
            value = getattr(payload, name, None)
        if value is not None:
            try:
                return max(int(value), 0)
            except (TypeError, ValueError):
                return 0
    return 0

