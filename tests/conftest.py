from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def task_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace" / "task-1"
    workspace.mkdir(parents=True)
    return workspace


@pytest.fixture
def context_path(task_workspace: Path) -> Path:
    path = task_workspace / "context.json"
    path.write_text(
        "\n".join(
            [
                "{",
                '  "task_id": "task-1",',
                '  "title": "fix: smoke",',
                '  "diff_content": "diff --git a/a.py b/a.py",',
                f'  "project_root": "{task_workspace}"',
                "}",
            ]
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def agent_config_path(task_workspace: Path, context_path: Path) -> Path:
    result_dir = task_workspace / "cr_result"
    path = task_workspace / "agent_config.toml"
    path.write_text(
        "\n".join(
            [
                'platform = "gitlab"',
                "",
                "[context]",
                f'json_path = "{context_path}"',
                f'result_path = "{result_dir}"',
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

