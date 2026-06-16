from __future__ import annotations

import pytest

from cr_agent.bootstrap import bootstrap_runtime
from cr_agent.skills.validate_json.skill import validate_json


@pytest.mark.asyncio
async def test_validate_json_accepts_minimal_report(agent_config_path) -> None:
    runtime_context = bootstrap_runtime(agent_config_path, platform_override=None)
    result = await validate_json(runtime_context, {"llm_result": "# ok"})
    assert result.valid is True
    assert result.errors == []


@pytest.mark.asyncio
async def test_validate_json_rejects_empty_llm_result(agent_config_path) -> None:
    runtime_context = bootstrap_runtime(agent_config_path, platform_override=None)
    result = await validate_json(runtime_context, {"llm_result": ""})
    assert result.valid is False
    assert any("llm_result" in error for error in result.errors)


@pytest.mark.asyncio
async def test_validate_json_rejects_invalid_severity(agent_config_path) -> None:
    runtime_context = bootstrap_runtime(agent_config_path, platform_override=None)
    result = await validate_json(
        runtime_context,
        {
            "llm_result": "# ok",
            "issues": [
                {
                    "severity": "blocker",
                    "title": "bad",
                    "count": 1,
                    "locations": [],
                }
            ],
        }
    )
    assert result.valid is False
    assert any("issues.0.severity" in error for error in result.errors)


@pytest.mark.asyncio
async def test_validate_json_rejects_invalid_line_ranges(agent_config_path) -> None:
    runtime_context = bootstrap_runtime(agent_config_path, platform_override=None)
    result = await validate_json(
        runtime_context,
        {
            "llm_result": "# ok",
            "line_comments": {
                "comments": [
                    {
                        "new_path": "a.py",
                        "body": "fix",
                        "start_line": 10,
                        "end_line": 9,
                    }
                ]
            },
        }
    )
    assert result.valid is False
    assert any("line_comments.comments.0.end_line" in error for error in result.errors)


@pytest.mark.asyncio
async def test_validate_json_rejects_comments_and_issues_count_mismatch(agent_config_path) -> None:
    runtime_context = bootstrap_runtime(agent_config_path, platform_override=None)
    result = await validate_json(
        runtime_context,
        {
            "llm_result": "# ok",
            "line_comments": {"comments": []},
            "issues": [
                {
                    "severity": "major",
                    "title": "missing comment",
                    "count": 1,
                    "locations": [{"path": "a.py", "start_line": 1, "end_line": 1}],
                }
            ],
        },
    )

    assert result.valid is False
    assert any("count must equal" in error for error in result.errors)
