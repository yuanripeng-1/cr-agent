from __future__ import annotations

from types import SimpleNamespace

import pytest

from cr_agent.core.types import QueryResult
from cr_agent.skills.summarize.skill import summarize_report


class _Runtime:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict] = []

    async def query_subagent(self, agent_name: str, prompt: str, *, assembled_options=None):
        self.calls.append(
            {
                "agent_name": agent_name,
                "prompt": prompt,
                "assembled_options": assembled_options,
            }
        )
        return QueryResult(text=self.text)


@pytest.mark.asyncio
async def test_summarize_report_calls_summary_subagent_without_tools() -> None:
    runtime = _Runtime('{"llm_result": "# report", "issues": []}')
    context = SimpleNamespace(summary_runtime=runtime)

    report = await summarize_report(
        context,
        {"task_id": "task-1"},
        [{"dimension": "security", "score": 90}],
        None,
    )

    assert report["llm_result"] == "# report"
    assert runtime.calls[0]["agent_name"] == "summary"
    assert runtime.calls[0]["assembled_options"] is None


@pytest.mark.asyncio
async def test_summarize_report_wraps_markdown_output() -> None:
    runtime = _Runtime("# markdown report")
    context = SimpleNamespace(summary_runtime=runtime)

    report = await summarize_report(context, {"task_id": "task-1"}, [], None)

    assert report["llm_result"] == "# markdown report"


@pytest.mark.asyncio
async def test_summarize_report_passes_validation_errors_to_prompt() -> None:
    runtime = _Runtime('{"llm_result": "# fixed"}')
    context = SimpleNamespace(summary_runtime=runtime)

    report = await summarize_report(
        context,
        {"task_id": "task-1"},
        [],
        ["llm_result: String should have at least 1 character"],
    )

    assert report["validation_errors"] == ["llm_result: String should have at least 1 character"]
    assert "validation_errors" in runtime.calls[0]["prompt"]
    assert "String should have at least 1 character" in runtime.calls[0]["prompt"]
