from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from cr_agent.bootstrap import bootstrap_runtime
from cr_agent.core.types import QueryResult
from cr_agent.skills.summarize.skill import summarize_report


class _Runtime:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls: list[dict] = []

    async def query_subagent(self, agent_name: str, prompt: str, *, assembled_options=None, timeout_s=None):
        self.calls.append(
            {
                "agent_name": agent_name,
                "prompt": prompt,
                "assembled_options": assembled_options,
            }
        )
        return QueryResult(text=self.text)


@pytest.mark.asyncio
async def test_summarize_report_calls_summary_subagent_without_tools(agent_config_path: Path) -> None:
    runtime = _Runtime('{"llm_result": "# report", "issues": []}')
    context = replace(bootstrap_runtime(agent_config_path, platform_override=None), summary_runtime=runtime)

    report = await summarize_report(
        context,
        {"task_id": "task-1"},
        [{"dimension": "security", "score": 90}],
        None,
    )

    assert report["llm_result"] == "# report"
    assert runtime.calls[0]["agent_name"] == "summary"
    assert runtime.calls[0]["assembled_options"] is None
    assert "# summarize_report" in runtime.calls[0]["prompt"]
    assert "Summary 评级规则" in runtime.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_summarize_report_wraps_markdown_output(agent_config_path: Path) -> None:
    runtime = _Runtime("# markdown report")
    context = replace(bootstrap_runtime(agent_config_path, platform_override=None), summary_runtime=runtime)

    report = await summarize_report(context, {"task_id": "task-1"}, [], None)

    assert report["llm_result"] == "# markdown report"


@pytest.mark.asyncio
async def test_summarize_report_passes_validation_errors_to_prompt(agent_config_path: Path) -> None:
    runtime = _Runtime('{"llm_result": "# fixed"}')
    context = replace(bootstrap_runtime(agent_config_path, platform_override=None), summary_runtime=runtime)

    report = await summarize_report(
        context,
        {"task_id": "task-1"},
        [],
        ["llm_result: String should have at least 1 character"],
    )

    assert report["validation_errors"] == ["llm_result: String should have at least 1 character"]
    assert "validation_errors" in runtime.calls[0]["prompt"]
    assert "String should have at least 1 character" in runtime.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_summarize_report_compacts_large_dimension_payload(agent_config_path: Path) -> None:
    runtime = _Runtime('{"llm_result": "# compact"}')
    context = replace(bootstrap_runtime(agent_config_path, platform_override=None), summary_runtime=runtime)
    huge_yaml = "raw-yaml-line\n" * 2000
    huge_raw = {"code_suggestion": "suggestion\n" * 2000}

    await summarize_report(
        context,
        {"task_id": "task-1", "changed_files": ["a.py"], "irrelevant": huge_yaml},
        [
            {
                "dimension": "security",
                "score": 90,
                "artifact_path": "/tmp/security.json",
                "raw_yaml": huge_yaml,
                "normalized_findings": [
                    {
                        "title": "issue",
                        "analysis": "analysis",
                        "suggestion": "fix",
                        "raw": huge_raw,
                    }
                ],
            }
        ],
        None,
    )

    prompt = runtime.calls[0]["prompt"]
    assert "raw_yaml" not in prompt
    assert "code_suggestion" not in prompt
    assert "irrelevant" not in prompt
    assert "artifact_path" in prompt
