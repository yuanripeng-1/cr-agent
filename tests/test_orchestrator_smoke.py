from __future__ import annotations

from pathlib import Path
from dataclasses import replace

import pytest

from cr_agent.bootstrap import bootstrap_runtime
from cr_agent.core.orchestrator import run_review
from cr_agent.core.review_output import load_review_result
from cr_agent.core.types import QueryResult, TokenUsage, ValidationResult
from cr_agent.skills.collect_context.skill import collect_context
from cr_agent.skills.registry import SkillRegistry
from cr_agent.tools.provider import ToolSpec
from tests.fakes import FakeSkillScenario, ScriptedMainAgentRuntime


@pytest.mark.asyncio
async def test_orchestrator_smoke_writes_success_artifacts(
    agent_config_path: Path,
) -> None:
    runtime_context = bootstrap_runtime(agent_config_path, platform_override=None)
    fake_runtime = ScriptedMainAgentRuntime(
        usage=TokenUsage(
            input_tokens=10,
            output_tokens=5,
            cache_creation_tokens=2,
            cache_read_tokens=3,
        )
    )

    result = await run_review(
        runtime_context,
        agent_runtime=fake_runtime,
        skill_registry=FakeSkillScenario().registry(),
    )

    assert result.status == "success"
    assert result.tokens_consume.input_tokens == 10
    assert result.tokens_consume.output_tokens == 5
    assert result.tokens_consume.cache_creation_tokens == 2
    assert result.tokens_consume.cache_read_tokens == 3
    assert (runtime_context.result_dir / "result.json").exists()
    assert (runtime_context.result_dir / "cr_result.md").exists()
    assert (runtime_context.result_dir / "run.log").exists()
    assert load_review_result(runtime_context.result_dir / "result.json").status == "success"


@pytest.mark.asyncio
async def test_main_agent_retries_summarize_on_validate_failure(
    agent_config_path: Path,
) -> None:
    runtime_context = bootstrap_runtime(agent_config_path, platform_override=None)
    scenario = FakeSkillScenario(validation_failures=1)
    fake_runtime = ScriptedMainAgentRuntime()

    result = await run_review(
        runtime_context,
        agent_runtime=fake_runtime,
        skill_registry=scenario.registry(),
        max_retries=2,
    )

    assert result.status == "success"
    # 主 agent 据 summarize 返回的 {valid:false} 重调了一次。
    assert scenario.summarize_calls == 2
    assert scenario.validate_calls == 2
    assert fake_runtime.summarize_tool_calls == 2
    assert fake_runtime.denied is False


@pytest.mark.asyncio
async def test_python_backstop_terminates_after_max_retries(
    agent_config_path: Path,
) -> None:
    runtime_context = bootstrap_runtime(agent_config_path, platform_override=None)
    scenario = FakeSkillScenario(validation_failures=10)
    fake_runtime = ScriptedMainAgentRuntime()

    result = await run_review(
        runtime_context,
        agent_runtime=fake_runtime,
        skill_registry=scenario.registry(),
        max_retries=1,
    )

    assert result.status == "failed"
    assert result.errors == ["invalid fake report"]
    # max_retries=1 → 允许 summarize 共 2 次,第 3 次被 Python 兜底拒绝。
    assert fake_runtime.summarize_tool_calls == 2
    assert fake_runtime.denied is True
    persisted = load_review_result(runtime_context.result_dir / "result.json")
    assert persisted.status == "failed"


@pytest.mark.asyncio
async def test_run_review_requires_agent_runtime(agent_config_path: Path) -> None:
    runtime_context = bootstrap_runtime(agent_config_path, platform_override=None)
    with pytest.raises(ValueError, match="main agent runtime"):
        await run_review(runtime_context, agent_runtime=None)


class _EmptyFacade:
    def tools_for(self, agent_name: str) -> list[ToolSpec]:
        assert agent_name == "context"
        return []


class _UsageContextRuntime:
    async def query_subagent(self, agent_name: str, prompt: str, *, assembled_options=None):
        assert agent_name == "context"
        return QueryResult(
            text='{"summary":"ctx","diff_summary":"diff","warnings":[]}',
            usage=TokenUsage(input_tokens=3, output_tokens=4),
        )


class _ValidSummaryScenario:
    def registry(self) -> SkillRegistry:
        return SkillRegistry(
            collect_context=collect_context,
            dimension_review=self.dimension_review,
            summarize_report=self.summarize_report,
            validate_json=self.validate_json,
        )

    async def dimension_review(self, collected_context: dict) -> list[dict]:
        return [{"dimension": "fake", "score": 100, "findings": []}]

    async def summarize_report(self, runtime_context, collected_context, dimension_scores, validation_errors):
        return {"llm_result": "# ok"}

    async def validate_json(self, report: dict) -> ValidationResult:
        return ValidationResult(valid=True)


@pytest.mark.asyncio
async def test_collect_context_usage_is_accumulated(agent_config_path: Path) -> None:
    runtime_context = bootstrap_runtime(agent_config_path, platform_override=None)
    runtime_context = replace(
        runtime_context,
        tool_facade=_EmptyFacade(),
        context_runtime=_UsageContextRuntime(),
    )
    fake_runtime = ScriptedMainAgentRuntime(usage=TokenUsage(input_tokens=10, output_tokens=5))

    result = await run_review(
        runtime_context,
        agent_runtime=fake_runtime,
        skill_registry=_ValidSummaryScenario().registry(),
    )

    assert result.tokens_consume.input_tokens == 13
    assert result.tokens_consume.output_tokens == 9
