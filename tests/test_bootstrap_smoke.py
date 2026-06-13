from __future__ import annotations

from pathlib import Path

from cr_agent.bootstrap import bootstrap_runtime


def test_bootstrap_smoke_builds_runtime_context(agent_config_path: Path) -> None:
    runtime_context = bootstrap_runtime(agent_config_path, platform_override=None)

    assert runtime_context.platform == "gitlab"
    assert runtime_context.review_input.task_id == "task-1"
    assert runtime_context.config.llm.model == "test-model"
    assert runtime_context.result_dir.name == "cr_result"

