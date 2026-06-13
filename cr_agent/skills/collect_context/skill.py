from __future__ import annotations

from typing import Any

from cr_agent.bootstrap import RuntimeContext


async def collect_context(runtime_context: RuntimeContext) -> dict[str, Any]:
    review_input = runtime_context.review_input
    return {
        "task_id": review_input.task_id,
        "title": review_input.title,
        "diff_content": review_input.diff_content,
        "project_root": review_input.project_root,
        "platform": runtime_context.platform,
    }
