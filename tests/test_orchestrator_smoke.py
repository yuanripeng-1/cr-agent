from __future__ import annotations

from pathlib import Path

import pytest

from cr_agent.bootstrap import bootstrap_runtime
from cr_agent.core.orchestrator import run_review
from cr_agent.core.review_output import load_review_result
from cr_agent.core.types import QueryResult, TokenUsage
from tests.fakes import FakeAgentRuntime, FakeSkillScenario


@pytest.mark.asyncio
async def test_orchestrator_smoke_writes_success_artifacts(
    agent_config_path: Path,
) -> None:
    runtime_context = bootstrap_runtime(agent_config_path, platform_override=None)
    fake_runtime = FakeAgentRuntime(
        results=[
            QueryResult(
                text="main ok",
                usage=TokenUsage(
                    input_tokens=10,
                    output_tokens=5,
                    cache_creation_tokens=2,
                    cache_read_tokens=3,
                ),
            )
        ]
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
async def test_orchestrator_retries_validate_failures(
    agent_config_path: Path,
) -> None:
    runtime_context = bootstrap_runtime(agent_config_path, platform_override=None)
    scenario = FakeSkillScenario(validation_failures=1)

    result = await run_review(
        runtime_context,
        agent_runtime=FakeAgentRuntime(),
        skill_registry=scenario.registry(),
        max_retries=2,
    )

    assert result.status == "success"
    assert scenario.summarize_calls == 2
    assert scenario.validate_calls == 2


@pytest.mark.asyncio
async def test_orchestrator_writes_terminal_artifact_after_max_retries(
    agent_config_path: Path,
) -> None:
    runtime_context = bootstrap_runtime(agent_config_path, platform_override=None)
    scenario = FakeSkillScenario(validation_failures=10)

    result = await run_review(
        runtime_context,
        agent_runtime=FakeAgentRuntime(),
        skill_registry=scenario.registry(),
        max_retries=1,
    )

    assert result.status == "failed"
    assert result.errors == ["invalid fake report"]
    persisted = load_review_result(runtime_context.result_dir / "result.json")
    assert persisted.status == "failed"

