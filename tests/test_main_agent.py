from __future__ import annotations

import logging
from pathlib import Path

import pytest

from cr_agent.bootstrap import bootstrap_runtime
from cr_agent.core.main_agent import (
    MainAgentSession,
    build_skill_tools,
    make_backstop_can_use_tool,
)
from cr_agent.core.state import ReviewState
from tests.fakes import FakeSkillScenario


def _session(max_retries: int) -> MainAgentSession:
    return MainAgentSession(
        runtime_context=None,  # backstop 只读 state,不需要真实 context
        registry=None,
        state=ReviewState(max_retries=max_retries),
    )


@pytest.mark.asyncio
async def test_backstop_allows_summarize_within_budget() -> None:
    session = _session(max_retries=2)  # 允许 3 次
    can_use_tool = make_backstop_can_use_tool(session)

    session.state.attempt = 0
    assert (await can_use_tool("mcp__skills__summarize_report", {}, None)).behavior == "allow"
    session.state.attempt = 2
    assert (await can_use_tool("mcp__skills__summarize_report", {}, None)).behavior == "allow"


@pytest.mark.asyncio
async def test_backstop_denies_summarize_beyond_max_retries() -> None:
    session = _session(max_retries=1)  # 允许 2 次,attempt>=2 拒绝
    can_use_tool = make_backstop_can_use_tool(session)

    session.state.attempt = 2
    result = await can_use_tool("mcp__skills__summarize_report", {}, None)
    assert result.behavior == "deny"


@pytest.mark.asyncio
async def test_backstop_allows_non_summarize_tools_always() -> None:
    session = _session(max_retries=0)
    can_use_tool = make_backstop_can_use_tool(session)

    session.state.attempt = 99
    for name in ("mcp__skills__collect_context", "mcp__skills__dimension_review"):
        assert (await can_use_tool(name, {}, None)).behavior == "allow"


@pytest.mark.asyncio
async def test_main_agent_skill_call_emits_skill_start_log(
    agent_config_path: Path, caplog
) -> None:
    runtime_context = bootstrap_runtime(agent_config_path, platform_override=None)
    session = MainAgentSession(
        runtime_context=runtime_context,
        registry=FakeSkillScenario().registry(),
        state=ReviewState(),
    )
    tools = {spec.name: spec for spec in build_skill_tools(session)}

    with caplog.at_level(logging.INFO, logger="cr_agent.core.main_agent"):
        await tools["collect_context"].handler({})

    messages = [r.getMessage() for r in caplog.records]
    assert any("SKILL_START skill=collect_context" in m for m in messages)
    assert session.collected_context is not None
