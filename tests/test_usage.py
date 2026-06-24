from __future__ import annotations

from types import SimpleNamespace

from cr_agent.core.types import TokenUsage
from cr_agent.core.usage import accumulate_usage, extract_usage


def test_accumulate_usage_adds_task_level_token_totals() -> None:
    total = accumulate_usage(
        TokenUsage(input_tokens=1, output_tokens=2, cache_creation_tokens=3, cache_read_tokens=4),
        TokenUsage(input_tokens=10, output_tokens=20, cache_creation_tokens=30, cache_read_tokens=40),
    )

    assert total == TokenUsage(
        input_tokens=11,
        output_tokens=22,
        cache_creation_tokens=33,
        cache_read_tokens=44,
    )


def test_extract_usage_tolerates_missing_usage() -> None:
    assert extract_usage({"text": "ok"}) == TokenUsage()


def test_extract_usage_reads_object_payload() -> None:
    raw = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=3,
            output_tokens=4,
            cache_creation_input_tokens=5,
            cache_read_input_tokens=6,
        )
    )

    assert extract_usage(raw) == TokenUsage(
        input_tokens=3,
        output_tokens=4,
        cache_creation_tokens=5,
        cache_read_tokens=6,
    )

