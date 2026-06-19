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
    prompt = runtime.calls[0]["prompt"]
    assert "# summarize_report" in prompt
    assert "summary subagent 不得使用任何工具" in prompt
    assert "汇总提示词" in prompt
    assert "汇总评级规则" in prompt
    assert "validation_errors" in prompt
    assert "请严格按照上方 SKILL.md、汇总提示词和汇总评级规则执行" in prompt


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
    assert "String should have at least 1 character" in runtime.calls[0]["prompt"]
    assert '"validation_errors"' not in runtime.calls[0]["prompt"]


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
                "title": "issue",
                "analysis": "analysis",
                "suggestion": "fix",
                "score": 90,
                "raw": huge_raw,
            }
        ],
        None,
    )

    prompt = runtime.calls[0]["prompt"]
    assert "raw_yaml" not in prompt
    assert "code_suggestion" not in prompt
    assert "irrelevant" not in prompt
    assert "artifact_path" not in prompt
    assert '"findings"' not in prompt
    assert "collected_context" not in prompt
    assert "raw_diff" not in prompt
    assert "结果过滤的评分后缺陷总结文件" in prompt


@pytest.mark.asyncio
async def test_summarize_report_writes_summary_prompt_artifact(agent_config_path: Path) -> None:
    runtime = _Runtime('{"llm_result": "# report"}')
    context = replace(bootstrap_runtime(agent_config_path, platform_override=None), summary_runtime=runtime)

    await summarize_report(context, {"task_id": "task-1"}, [], None)

    prompt_path = context.result_dir / "summary_prompt.txt"
    assert prompt_path.exists()
    assert prompt_path.read_text(encoding="utf-8") == runtime.calls[0]["prompt"]
