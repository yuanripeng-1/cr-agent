from __future__ import annotations

from types import SimpleNamespace

from cr_agent.core.types import TokenUsage
from cr_agent.core.usage import accumulate_usage, accumulate_usage_from_dimension_artifacts, extract_usage


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


def test_accumulate_usage_from_dimension_artifacts_includes_failed_nonzero_usage(tmp_path) -> None:
    dimensions_dir = tmp_path / "dimensions"
    dimensions_dir.mkdir()
    (dimensions_dir / "security.json").write_text(
        """
        {
          "dimension": "security",
          "status": "failed",
          "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "cost": 0.1
          }
        }
        """,
        encoding="utf-8",
    )
    (dimensions_dir / "testing.json").write_text(
        """
        {
          "dimension": "testing",
          "status": "failed",
          "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_tokens": 0,
            "cache_read_tokens": 0,
            "cost": 0.0
          }
        }
        """,
        encoding="utf-8",
    )
    usages = []
    entries = []

    accumulate_usage_from_dimension_artifacts(
        dimensions_dir,
        add_usage=usages.append,
        add_breakdown=entries.append,
    )

    assert usages == [TokenUsage(input_tokens=10, output_tokens=5, cost=0.1)]
    assert len(entries) == 1
    assert entries[0]["dimension"] == "security"
    assert entries[0]["status"] == "failed"
