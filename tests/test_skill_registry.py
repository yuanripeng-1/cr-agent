from __future__ import annotations

from dataclasses import replace

import pytest

from cr_agent.bootstrap import bootstrap_runtime
from cr_agent.core.types import QueryResult
from cr_agent.skills.registry import build_default_skill_registry, registered_skill_names


class _SummaryRuntime:
    async def query_subagent(self, agent_name: str, prompt: str, *, assembled_options=None, timeout_s=None):
        assert agent_name == "summary"
        assert assembled_options is None
        return QueryResult(text='{"llm_result": "# CR-Agent\\n\\nGenerated."}')


class _ContextRuntime:
    async def query_subagent(self, agent_name: str, prompt: str, *, assembled_options=None, timeout_s=None):
        assert agent_name == "context"
        return QueryResult(
            text='{"summary":"context collected","diff_summary":"diff ok","warnings":[]}'
        )


class _DimensionRuntime:
    async def query_subagent(self, agent_name: str, prompt: str, *, assembled_options=None, timeout_s=None):
        assert agent_name == "dimension"
        return QueryResult(
            text="review:\n  dimension: business\n  score: 90\n  confidence: 80\n  findings: []\n"
        )


class _EmptyFacade:
    def tools_for(self, agent_name: str):
        return []


def test_default_registry_exposes_expected_skill_names() -> None:
    assert registered_skill_names() == {
        "collect_context",
        "dimension_review",
        "summarize_report",
        "validate_json",
    }


@pytest.mark.asyncio
async def test_default_registry_placeholder_flow(agent_config_path) -> None:
    runtime_context = bootstrap_runtime(agent_config_path, platform_override=None)
    runtime_context = replace(
        runtime_context,
        summary_runtime=_SummaryRuntime(),
        context_runtime=_ContextRuntime(),
        dimension_runtime=_DimensionRuntime(),
        tool_facade=_EmptyFacade(),
    )
    registry = build_default_skill_registry()

    context = await registry.collect_context(runtime_context)
    scores = await registry.dimension_review(runtime_context, context)
    report = await registry.summarize_report(runtime_context, context, scores, None)
    validation = await registry.validate_json(runtime_context, report)

    assert context["task_id"] == "task-1"
    assert scores[0]["dimension"] == "business"
    assert report["llm_result"].startswith("# CR-Agent")
    assert validation.valid is True
