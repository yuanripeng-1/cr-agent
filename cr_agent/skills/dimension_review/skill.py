from __future__ import annotations

from typing import Any


async def dimension_review(collected_context: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "dimension": "placeholder",
            "score": 100,
            "confidence": 1,
            "findings": [],
            "context_task_id": collected_context.get("task_id", ""),
        }
    ]
