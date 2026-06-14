from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from cr_agent.core.agent_config import AgentConfig
from cr_agent.core.review_input import ReviewInput
from cr_agent.core.review_output import IssueLocation, LineComment, ReviewResult
from cr_agent.core.verify_contracts import main as verify_contracts_main


def test_review_input_allows_missing_commit_messages() -> None:
    review_input = ReviewInput.model_validate(
        {
            "task_id": "task-1",
            "title": "fix: sample",
            "diff_content": "diff --git a/a.py b/a.py",
            "project_root": "/tmp/project",
        }
    )

    assert review_input.commit_messages == []


def test_review_input_accepts_requirement_aliases() -> None:
    review_input = ReviewInput.model_validate(
        {
            "task_id": "task-1",
            "title": "fix: sample",
            "diff_content": "diff --git a/a.py b/a.py",
            "project_root": "/tmp/project",
            "requirements_Doc": "/tmp/req.md",
        }
    )

    assert review_input.requirements_doc == "/tmp/req.md"


def test_review_input_requirement_alias_conflict_uses_backend_field() -> None:
    review_input = ReviewInput.model_validate(
        {
            "task_id": "task-1",
            "title": "fix: sample",
            "diff_content": "diff --git a/a.py b/a.py",
            "project_root": "/tmp/project",
            "requirements_Doc": "REAL_VALUE",
            "requirements_doc": "DIFFERENT_VALUE",
        }
    )

    assert review_input.requirements_doc == "REAL_VALUE"
    assert review_input.model_dump()["requirements_doc"] == "REAL_VALUE"


def test_agent_config_platform_fallback_and_context_strictness() -> None:
    config = AgentConfig.model_validate(
        {
            "context": {"json_path": "context.json"},
            "llm": {"model": "test-model", "platform": "gitlab"},
        }
    )

    assert config.configured_platform() == "gitlab"
    assert config.tools.crg.enabled is False
    assert config.tools.crg.target_root == ""

    with pytest.raises(ValidationError):
        AgentConfig.model_validate(
            {
                "context": {"json_path": "context.json", "json_pat": "typo"},
                "llm": {"model": "test-model"},
            }
        )


def test_review_output_validates_line_ranges() -> None:
    with pytest.raises(ValidationError):
        LineComment(
            new_path="a.py",
            body="body",
            start_line=10,
            end_line=9,
        )

    with pytest.raises(ValidationError):
        IssueLocation(path="a.py", start_line=10, end_line=9)


def test_review_result_accepts_success_shape() -> None:
    result = ReviewResult.model_validate(
        {
            "llm_result": "# report",
            "status": "success",
            "log_path": "run.log",
            "tokens_consume": {
                "input_tokens": 1,
                "output_tokens": 2,
                "cost": 0.0,
            },
            "line_comments": {"comments": []},
            "issues": [],
        }
    )

    assert result.status == "success"


def test_verify_contracts_reports_readable_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_path = tmp_path / "agent_config.toml"
    context_path = tmp_path / "context.json"
    result_path = tmp_path / "result.json"

    config_path.write_text(
        "\n".join(
            [
                "[context]",
                f'json_path = "{context_path}"',
                "[llm]",
                'model = "test-model"',
                'platform = "gitlab"',
            ]
        ),
        encoding="utf-8",
    )
    context_path.write_text('{"task_id": ""}', encoding="utf-8")
    result_path.write_text(
        json.dumps(
            {
                "llm_result": "# report",
                "status": "success",
                "log_path": "run.log",
                "tokens_consume": {
                    "input_tokens": 1,
                    "output_tokens": 2,
                    "cost": 0.0,
                },
                "line_comments": {"comments": []},
                "issues": [],
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "sys.argv",
        [
            "verify_contracts",
            "--config",
            str(config_path),
            "--context",
            str(context_path),
            "--result",
            str(result_path),
        ],
    )

    assert verify_contracts_main() == 1
    output = capsys.readouterr().out
    assert "[contracts] failed: context contract failed" in output
    assert "Traceback" not in output
