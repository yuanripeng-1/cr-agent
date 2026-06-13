from __future__ import annotations

from typing import Any


async def summarize_report(
    collected_context: dict[str, Any],
    dimension_scores: list[dict[str, Any]],
    validation_errors: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "llm_result": (
            "# CR-Agent\n\n"
            "Placeholder review completed.\n\n"
            f"- task_id: `{collected_context.get('task_id', '')}`\n"
            f"- dimensions: `{len(dimension_scores)}`\n"
        ),
        "validation_errors": validation_errors or [],
    }
