from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from cr_agent.core.review_output import load_review_result


def test_main_bootstrap_only_writes_bootstrap_ready(agent_config_path: Path, monkeypatch) -> None:
    from cr_agent import main as main_module

    monkeypatch.setattr(
        "sys.argv",
        ["cr_agent.main", "--config", str(agent_config_path), "--bootstrap-only"],
    )

    assert main_module.main() == 0

    result_dir = agent_config_path.parent / "cr_result"
    result = load_review_result(result_dir / "result.json")
    assert result.status == "bootstrap_ready"


def test_main_default_runs_review(agent_config_path: Path, monkeypatch) -> None:
    from cr_agent import main as main_module
    from cr_agent.core.main_agent import MainAgentResult

    class _Runtime:
        async def run_review_loop(self, **kwargs):
            tools = {tool.name: tool for tool in kwargs["skill_tools"]}
            await tools["collect_context"].handler({})
            await tools["dimension_review"].handler({})
            await tools["summarize_report"].handler({})
            return MainAgentResult(text="done")

    async def collect_context(runtime_context):
        return {"task_id": runtime_context.review_input.task_id}

    async def dimension_review(runtime_context, collected_context):
        return [{"dimension": "fake", "score": 100, "usage": {}}]

    async def summarize_report(runtime_context, collected_context, dimension_scores, validation_errors):
        return {"llm_result": "# ok", "line_comments": {"comments": []}, "issues": []}

    async def validate_json(runtime_context, report):
        from cr_agent.core.types import ValidationResult

        return ValidationResult(valid=True)

    registry = SimpleNamespace(
        collect_context=collect_context,
        dimension_review=dimension_review,
        summarize_report=summarize_report,
        validate_json=validate_json,
    )

    async def fake_run_review(runtime_context, *, agent_runtime, **kwargs):
        from cr_agent.core.orchestrator import run_review

        return await run_review(
            runtime_context,
            agent_runtime=agent_runtime,
            skill_registry=registry,
        )

    monkeypatch.setattr(main_module, "build_main_agent_runtime", lambda config: _Runtime())
    monkeypatch.setattr(main_module, "run_review", fake_run_review)
    monkeypatch.setattr("sys.argv", ["cr_agent.main", "--config", str(agent_config_path)])

    assert main_module.main() == 0

    result = load_review_result(agent_config_path.parent / "cr_result" / "result.json")
    assert result.status == "success"
