from __future__ import annotations

import json
from pathlib import Path

import pytest
from cr_agent.issue_resolution.agent import parse_resolution_response
from cr_agent.issue_resolution.bootstrap import bootstrap_issue_resolution
from cr_agent.issue_resolution.contracts import (
    IssueResolutionInput,
    IssueResolutionResult,
    load_issue_resolution_input,
    load_issue_resolution_result,
    normalize_agent_results,
)
from cr_agent.issue_resolution.orchestrator import run_resolution_check


@pytest.fixture
def resolution_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "issue_resolution" / "42-abc12345"
    workspace.mkdir(parents=True)
    return workspace


@pytest.fixture
def resolution_context_path(resolution_workspace: Path, tmp_path: Path) -> Path:
    path = resolution_workspace / "issue_resolution_context.json"
    path.write_text(
        json.dumps(
            {
                "task_id": "42-abc12345",
                "project_id": 99,
                "mr_iid": 42,
                "mr_url": "https://gitlab.example/mr/42",
                "source_branch": "feature/fix",
                "target_branch": "main",
                "merged_commit_sha": "abc1234567890",
                "issues": [
                    {
                        "line_review_id": 101,
                        "discussion_id": "d1",
                        "note_id": 1,
                        "file_path": "src/a.py",
                        "line_number": 10,
                        "severity": "major",
                        "comment_body": "Missing nil check before dereference.",
                        "head_sha": "deadbeef",
                        "comment_url": "",
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


@pytest.fixture
def resolution_config_path(
    resolution_workspace: Path,
    resolution_context_path: Path,
    tmp_path: Path,
) -> Path:
    result_dir = tmp_path / "issue_resolution_result" / "42-abc12345"
    project_root = tmp_path / "project_code"
    project_root.mkdir(parents=True)
    path = resolution_workspace / "agent_config.toml"
    path.write_text(
        "\n".join(
            [
                'platform = "gitlab"',
                "",
                "[context]",
                f'json_path = "{resolution_context_path}"',
                f'result_path = "{result_dir}"',
                "",
                "[project]",
                f'project_root = "{project_root}"',
                "",
                "[llm]",
                'model = "test-model"',
                'api_key = "test-key"',
                'api_base = "http://litellm.invalid"',
            ]
        ),
        encoding="utf-8",
    )
    return path


def test_load_issue_resolution_input(resolution_context_path: Path) -> None:
    loaded = load_issue_resolution_input(resolution_context_path)
    assert loaded.task_id == "42-abc12345"
    assert loaded.mr_iid == 42
    assert len(loaded.issues) == 1
    assert loaded.issues[0].line_review_id == 101


def test_bootstrap_issue_resolution(resolution_config_path: Path) -> None:
    runtime = bootstrap_issue_resolution(resolution_config_path, platform_override=None)
    assert runtime.platform == "gitlab"
    assert runtime.resolution_input.task_id == "42-abc12345"
    assert runtime.project_root.name == "project_code"
    assert runtime.tool_facade.agent_tools["resolution"] == [
        "read_file_range",
        "grep_text",
        "git_rev_parse",
        "git_status",
    ]


def test_parse_resolution_response_extracts_json() -> None:
    text = (
        "analysis done\n```json\n"
        '{"results":[{"line_review_id":101,"state":"solved","reason":"added nil check","confidence":0.9}]}'
        "\n```"
    )
    parsed = parse_resolution_response(text, known_line_review_ids={101})
    assert len(parsed) == 1
    assert parsed[0].state == "solved"
    assert parsed[0].confidence == 0.9


def test_normalize_agent_results_filters_invalid_items() -> None:
    raw = [
        {"line_review_id": 101, "state": "solved", "reason": "ok", "confidence": 0.5},
        {"line_review_id": 999, "state": "solved", "reason": "unknown", "confidence": 0.5},
        {"line_review_id": 102, "state": "dismissed", "reason": "bad", "confidence": 0.5},
        {"line_review_id": 103, "state": "open", "reason": "still bad", "confidence": 1.5},
    ]
    normalized = normalize_agent_results(raw, known_line_review_ids={101, 102, 103})
    assert len(normalized) == 1
    assert normalized[0].line_review_id == 101


@pytest.mark.asyncio
async def test_run_resolution_check_writes_success(
    resolution_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = bootstrap_issue_resolution(resolution_config_path, platform_override=None)

    async def fake_run_resolution_agent(ctx):
        assert ctx.resolution_input.issues[0].comment_body.startswith("Missing nil")
        return parse_resolution_response(
            json.dumps(
                {
                    "results": [
                        {
                            "line_review_id": 101,
                            "state": "solved",
                            "reason": "nil guard added",
                            "confidence": 0.88,
                        }
                    ]
                }
            ),
            known_line_review_ids={101},
        )

    monkeypatch.setattr(
        "cr_agent.issue_resolution.orchestrator.run_resolution_agent",
        fake_run_resolution_agent,
    )

    result = await run_resolution_check(runtime)
    assert result.status == "success"
    assert len(result.results) == 1
    assert result.results[0].line_review_id == 101

    output_path = runtime.result_dir / "issue_resolution_result.json"
    loaded = load_issue_resolution_result(output_path)
    assert loaded.status == "success"
    assert loaded.results[0].state == "solved"


@pytest.mark.asyncio
async def test_run_resolution_check_empty_issues_writes_success(
    resolution_config_path: Path,
    resolution_context_path: Path,
) -> None:
    resolution_context_path.write_text(
        json.dumps(
            {
                "task_id": "42-abc12345",
                "project_id": 99,
                "mr_iid": 42,
                "source_branch": "feature/fix",
                "target_branch": "main",
                "merged_commit_sha": "abc1234567890",
                "issues": [],
            }
        ),
        encoding="utf-8",
    )
    runtime = bootstrap_issue_resolution(resolution_config_path, platform_override=None)

    result = await run_resolution_check(runtime)
    assert result.status == "success"
    assert result.results == []


@pytest.mark.asyncio
async def test_run_resolution_check_writes_failed_on_agent_error(
    resolution_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = bootstrap_issue_resolution(resolution_config_path, platform_override=None)

    async def _fail(_ctx):
        raise RuntimeError("agent exploded")

    monkeypatch.setattr(
        "cr_agent.issue_resolution.orchestrator.run_resolution_agent",
        _fail,
    )

    result = await run_resolution_check(runtime)
    assert result.status == "failed"
    assert result.errors
    loaded = load_issue_resolution_result(
        runtime.result_dir / "issue_resolution_result.json"
    )
    assert loaded.status == "failed"


def test_issue_resolution_main_success(
    resolution_config_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cr_agent.issue_resolution import main as resolution_main
    from cr_agent.issue_resolution.contracts import IssueResolutionItemResult

    async def fake_run_resolution_check(runtime_context):
        return IssueResolutionResult(
            task_id=runtime_context.resolution_input.task_id,
            status="success",
            results=[
                IssueResolutionItemResult(
                    line_review_id=101,
                    state="open",
                    reason="still unsafe",
                    confidence=0.7,
                )
            ],
            errors=[],
        )

    monkeypatch.setattr(resolution_main, "run_resolution_check", fake_run_resolution_check)
    monkeypatch.setattr(
        "sys.argv",
        [
            "cr_agent.issue_resolution.main",
            "--config",
            str(resolution_config_path),
        ],
    )

    assert resolution_main.main() == 0
