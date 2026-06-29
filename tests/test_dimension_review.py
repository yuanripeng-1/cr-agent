from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from cr_agent.bootstrap import bootstrap_runtime
from cr_agent.core.orchestrator import run_review
from cr_agent.core.review_output import load_review_result
from cr_agent.core.types import QueryResult, TokenUsage
from cr_agent.skills.dimension_review import skill as dimension_skill
from cr_agent.skills.dimension_review.skill import (
    _bounded_int,
    _normalize_findings,
    _wrap_dimension_tool_budget,
    dimension_review,
)
from cr_agent.skills.registry import SkillRegistry
from cr_agent.tools.provider import ToolSpec, ok_result
from tests.fakes import ScriptedMainAgentRuntime


class _EmptyFacade:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def tools_for(self, agent_name: str) -> list[ToolSpec]:
        self.calls.append(agent_name)
        return []


class _DimensionRuntime:
    def __init__(
        self,
        *,
        fail: set[str] | None = None,
        non_json: set[str] | None = None,
        delays: dict[str, float] | None = None,
        usage_by_dimension: dict[str, TokenUsage] | None = None,
    ) -> None:
        self.fail = fail or set()
        self.non_json = non_json or set()
        self.delays = delays or {}
        self.usage_by_dimension = usage_by_dimension or {}
        self.calls: list[dict[str, Any]] = []
        self.inflight = 0
        self.max_inflight = 0

    async def query_subagent(self, agent_name: str, prompt: str, *, assembled_options=None, timeout_s=None):
        dimension = _dimension_from_prompt(prompt)
        self.calls.append(
            {
                "agent_name": agent_name,
                "dimension": dimension,
                "prompt": prompt,
                "assembled_options": assembled_options,
            }
        )
        self.inflight += 1
        self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            delay = self.delays.get(dimension, 0)
            if delay:
                await asyncio.sleep(delay)
            if dimension in self.fail:
                raise RuntimeError(f"{dimension} exploded")
            if dimension in self.non_json:
                return QueryResult(text="- not an object")
            return QueryResult(
                text=(
                    "review:\n"
                    f"  dimension: {dimension}\n"
                    "  score: 90\n"
                    "  confidence: 80\n"
                    "  findings: []\n"
                ),
                usage=self.usage_by_dimension.get(
                    dimension,
                    TokenUsage(input_tokens=1, output_tokens=2),
                ),
            )
        finally:
            self.inflight -= 1


def _dimension_from_prompt(prompt: str) -> str:
    marker = "以下是本次运行输入："
    start = prompt.index(marker) + len(marker)
    payload_text = prompt[start:].split("\n\n请严格按照", 1)[0].strip()
    return str(json.loads(payload_text)["dimension"])


def _load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _runtime_context(agent_config_path: Path, *, platform: str, dimension_runtime: _DimensionRuntime):
    runtime_context = bootstrap_runtime(agent_config_path, platform_override=platform)
    return replace(
        runtime_context,
        tool_facade=_EmptyFacade(),
        dimension_runtime=dimension_runtime,
    )


def _write_dimensions_config(path: Path, *, dimensions: list[str], concurrency: Any) -> None:
    quoted = ", ".join(json.dumps(item) for item in dimensions)
    path.write_text(
        "\n".join(
            [
                "[defaults]",
                f"concurrency = {json.dumps(concurrency)}",
                "",
                "[profiles.gitlab]",
                f"dimensions = [{quoted}]",
                "",
                "[profiles.infcode]",
                "dimensions = []",
                "",
                "[defaults.fallback]",
                'dimensions = ["business"]',
            ]
        ),
        encoding="utf-8",
    )


def _real_gitlab_dimensions() -> list[str]:
    return dimension_skill._load_dimensions("gitlab").dimensions


def test_normalize_findings_preserves_source_lines_above_100() -> None:
    report = {
        "score": 72,
        "vulnerabilities": [
            {
                "file_path": "internal/storage/mysql/mysql.go",
                "start_line": 437,
                "end_line": 439,
                "description": "SQL injection",
                "score": 95,
            }
        ],
    }

    findings = _normalize_findings("security", report)

    assert findings[0]["start_line"] == 437
    assert findings[0]["end_line"] == 439
    assert findings[0]["score"] == 95


def test_normalize_findings_line_numbers_and_scores_use_separate_bounds() -> None:
    report = {
        "score": 72,
        "findings": [
            {"title": "zero line", "start_line": 0, "end_line": 0, "score": 150},
            {"title": "bad line", "start_line": "abc", "end_line": "abc", "score": -5},
        ],
    }

    findings = _normalize_findings("security", report)

    assert findings[0]["start_line"] == 0
    assert findings[0]["end_line"] == 0
    assert findings[0]["score"] == 100
    assert findings[1]["start_line"] == 0
    assert findings[1]["end_line"] == 0
    assert findings[1]["score"] == 0
    assert _bounded_int(150, default=0) == 100
    assert _bounded_int(-5, default=0) == 0


@pytest.mark.asyncio
async def test_dimension_review_gitlab_runs_all_dimensions_in_config_order(agent_config_path: Path) -> None:
    dimension_runtime = _DimensionRuntime()
    runtime_context = _runtime_context(
        agent_config_path,
        platform="gitlab",
        dimension_runtime=dimension_runtime,
    )

    scores = await dimension_review(runtime_context, {"task_id": "task-1"})

    expected = _real_gitlab_dimensions()
    assert [call["agent_name"] for call in dimension_runtime.calls] == ["dimension"] * len(expected)
    assert scores == []

    artifact_dir = runtime_context.result_dir / "dimensions"
    manifest = _load_json(artifact_dir / "manifest.json")
    assert manifest["status"] == "success"
    assert manifest["total"] == len(expected)
    assert manifest["succeeded"] == len(expected)
    assert manifest["failed"] == 0
    assert [entry["dimension"] for entry in manifest["dimensions"]] == expected
    for dimension in expected:
        artifact = _load_json(artifact_dir / f"{dimension}.json")
        assert artifact["dimension"] == dimension
        assert artifact["status"] == "success"
        assert "raw_yaml" in artifact


@pytest.mark.asyncio
async def test_dimension_review_respects_configured_concurrency_limit(
    agent_config_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    dimensions_config = tmp_path / "dimensions.toml"
    _write_dimensions_config(
        dimensions_config,
        dimensions=["business", "security", "performance", "testing"],
        concurrency=2,
    )
    monkeypatch.setattr(dimension_skill, "_DIMENSIONS_CONFIG", dimensions_config)
    dimension_runtime = _DimensionRuntime(
        delays={
            "business": 0.01,
            "security": 0.01,
            "performance": 0.01,
            "testing": 0.01,
        }
    )
    runtime_context = _runtime_context(
        agent_config_path,
        platform="gitlab",
        dimension_runtime=dimension_runtime,
    )

    with caplog.at_level("INFO", logger="cr_agent.skills.dimension_review"):
        scores = await dimension_review(runtime_context, {"task_id": "task-1"})

    assert scores == []
    assert dimension_runtime.max_inflight == 2
    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "DIMENSION_CONCURRENCY configured=2 effective=2 total=4" in message
        for message in messages
    )
    prompt = dimension_runtime.calls[0]["prompt"]
    assert "# dimension_review" in prompt
    assert "维度提示词" in prompt
    assert "维度评分规则" in prompt
    assert "只返回有效 YAML" in prompt
    assert "path` 必须是相对 `project_root`" in prompt
    assert "默认每个维度最多输出 3 个 high-confidence findings" in prompt
    assert "不提供 `read_file`" in prompt
    assert "available_tools" not in prompt


@pytest.mark.asyncio
async def test_dimension_review_prompt_uses_compact_context_without_tool_evidence(
    agent_config_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dimensions_config = tmp_path / "dimensions.toml"
    _write_dimensions_config(dimensions_config, dimensions=["business"], concurrency=1)
    monkeypatch.setattr(dimension_skill, "_DIMENSIONS_CONFIG", dimensions_config)
    dimension_runtime = _DimensionRuntime()
    runtime_context = _runtime_context(
        agent_config_path,
        platform="gitlab",
        dimension_runtime=dimension_runtime,
    )
    collected_context = {
        "task_id": "task-1",
        "artifact_path": "/tmp/collected_context.json",
        "summary": "context summary",
        "diff_summary": "diff summary",
        "changed_files": [{"path": "app.py", "new_path": "app.py", "added_ranges": [[1, 3]]}],
        "semantic_context": [{"summary": "semantic hit", "why_relevant": "touches changed function"}],
        "call_graph_context": [{"summary": "caller hit"}],
        "code_snippets": [{"path": "app.py", "start_line": 1, "end_line": 3, "excerpt": "def f(): pass"}],
        "warnings": [],
    }

    await dimension_review(runtime_context, collected_context)

    prompt = dimension_runtime.calls[0]["prompt"]
    assert "diff_summary" in prompt
    assert "added_ranges" in prompt
    assert "semantic hit" in prompt
    assert "def f(): pass" in prompt
    assert "tool_evidence" not in prompt


@pytest.mark.asyncio
async def test_dimension_tool_content_budget_is_independent_per_dimension() -> None:
    async def handler(args):
        return ok_result({"path": "a.py", "content": "x" * 8})

    tools = [
        ToolSpec(
            name="read_file_range",
            description="fake",
            input_schema={"type": "object"},
            handler=handler,
        )
    ]
    first = _wrap_dimension_tool_budget(tools, budget_bytes=10)[0]
    second = _wrap_dimension_tool_budget(tools, budget_bytes=10)[0]

    first_ok = await first.handler({})
    first_truncated = await first.handler({})
    second_ok = await second.handler({})

    assert first_ok["ok"] is True
    assert len(first_ok["data"]["content"]) == 8
    assert first_truncated["ok"] is True
    assert len(first_truncated["data"]["content"]) == 2
    assert any("content budget" in w for w in first_truncated["warnings"])
    assert second_ok["ok"] is True
    assert len(second_ok["data"]["content"]) == 8


@pytest.mark.asyncio
async def test_dimension_content_budget_does_not_block_non_content_tools() -> None:
    async def content_handler(args):
        return ok_result({"path": "a.py", "content": "x" * 10})

    async def grep_handler(args):
        return ok_result({"pattern": "needle", "matches": ["a.py:1:needle"]})

    tools = [
        ToolSpec(
            name="read_file_range",
            description="fake range",
            input_schema={"type": "object"},
            handler=content_handler,
        ),
        ToolSpec(
            name="grep_text",
            description="fake grep",
            input_schema={"type": "object"},
            handler=grep_handler,
        ),
    ]
    wrapped = {tool.name: tool for tool in _wrap_dimension_tool_budget(tools, budget_bytes=5)}

    truncated = await wrapped["read_file_range"].handler({})
    grep = await wrapped["grep_text"].handler({})

    assert truncated["ok"] is True
    assert len(truncated["data"]["content"]) == 5
    assert any("content budget" in warning for warning in truncated["warnings"])
    assert grep["ok"] is True
    assert grep["data"]["matches"] == ["a.py:1:needle"]


@pytest.mark.asyncio
async def test_dimension_review_infcode_uses_fallback_when_profile_empty(
    agent_config_path: Path,
) -> None:
    dimension_runtime = _DimensionRuntime()
    runtime_context = _runtime_context(
        agent_config_path,
        platform="infcode",
        dimension_runtime=dimension_runtime,
    )

    scores = await dimension_review(runtime_context, {"task_id": "task-1"})

    assert scores == []
    manifest = _load_json(runtime_context.result_dir / "dimensions" / "manifest.json")
    assert manifest["fallback_used"] is True
    assert manifest["status"] == "success"


@pytest.mark.asyncio
async def test_dimension_review_infcode_prefers_configured_subset(
    agent_config_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dimensions_config = tmp_path / "dimensions.toml"
    dimensions_config.write_text(
        "\n".join(
            [
                "[profiles.gitlab]",
                'dimensions = ["business"]',
                "",
                "[profiles.infcode]",
                'dimensions = ["security", "testing"]',
                "",
                "[defaults.fallback]",
                'dimensions = ["business"]',
            ]
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(dimension_skill, "_DIMENSIONS_CONFIG", dimensions_config)
    dimension_runtime = _DimensionRuntime()
    runtime_context = _runtime_context(
        agent_config_path,
        platform="infcode",
        dimension_runtime=dimension_runtime,
    )

    scores = await dimension_review(runtime_context, {"task_id": "task-1"})

    assert scores == []
    manifest = _load_json(runtime_context.result_dir / "dimensions" / "manifest.json")
    assert manifest["fallback_used"] is False
    assert manifest["configured_dimensions"] == ["security", "testing"]


@pytest.mark.asyncio
async def test_dimension_failure_degrades_without_failing_whole_task(
    agent_config_path: Path,
) -> None:
    dimension_runtime = _DimensionRuntime(fail={"security"}, non_json={"performance"})
    runtime_context = _runtime_context(
        agent_config_path,
        platform="gitlab",
        dimension_runtime=dimension_runtime,
    )

    scores = await dimension_review(runtime_context, {"task_id": "task-1"})

    assert scores == []
    artifact_dir = runtime_context.result_dir / "dimensions"
    manifest = _load_json(artifact_dir / "manifest.json")
    assert manifest["status"] == "degraded"
    expected_total = len(_real_gitlab_dimensions())
    assert manifest["succeeded"] == expected_total - 2
    assert manifest["failed"] == 2
    assert _load_json(artifact_dir / "security.json")["status"] == "failed"
    performance_artifact = _load_json(artifact_dir / "performance.json")
    assert performance_artifact["status"] == "failed"
    assert performance_artifact["raw_yaml"] == "- not an object"


@pytest.mark.asyncio
async def test_dimension_manifest_stays_consistent_when_concurrent_tasks_finish_out_of_order(
    agent_config_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dimensions_config = tmp_path / "dimensions.toml"
    expected = ["business", "security", "performance", "testing"]
    _write_dimensions_config(dimensions_config, dimensions=expected, concurrency=4)
    monkeypatch.setattr(dimension_skill, "_DIMENSIONS_CONFIG", dimensions_config)
    dimension_runtime = _DimensionRuntime(
        fail={"performance"},
        delays={
            "business": 0.03,
            "security": 0.01,
            "performance": 0.02,
            "testing": 0,
        },
    )
    runtime_context = _runtime_context(
        agent_config_path,
        platform="gitlab",
        dimension_runtime=dimension_runtime,
    )

    scores = await dimension_review(runtime_context, {"task_id": "task-1"})

    assert scores == []
    artifact_dir = runtime_context.result_dir / "dimensions"
    manifest = _load_json(artifact_dir / "manifest.json")
    assert manifest["status"] == "degraded"
    assert manifest["succeeded"] == 3
    assert manifest["failed"] == 1
    assert [entry["dimension"] for entry in manifest["dimensions"]] == expected
    for dimension in expected:
        assert (artifact_dir / f"{dimension}.json").exists()


@pytest.mark.asyncio
async def test_dimension_manifest_status_failed_when_all_dimensions_fail(
    agent_config_path: Path,
) -> None:
    dimension_runtime = _DimensionRuntime(fail=set(_real_gitlab_dimensions()))
    runtime_context = _runtime_context(
        agent_config_path,
        platform="gitlab",
        dimension_runtime=dimension_runtime,
    )

    with pytest.raises(Exception, match="all dimension reviews failed"):
        await dimension_review(runtime_context, {"task_id": "task-1"})

    manifest = _load_json(runtime_context.result_dir / "dimensions" / "manifest.json")
    assert manifest["status"] == "failed"
    assert manifest["succeeded"] == 0
    assert manifest["failed"] == len(_real_gitlab_dimensions())


class _EmptyContextRuntime:
    async def query_subagent(self, agent_name: str, prompt: str, *, assembled_options=None, timeout_s=None):
        assert agent_name == "context"
        return QueryResult(text='{"summary":"ctx","diff_summary":"diff","warnings":[]}')


class _FacadeForContextAndDimension:
    def tools_for(self, agent_name: str) -> list[ToolSpec]:
        assert agent_name in {"context", "dimension"}
        return []


class _RetrySummaryScenario:
    def __init__(self) -> None:
        self.summarize_calls = 0
        self.validate_calls = 0

    def registry(self) -> SkillRegistry:
        from cr_agent.skills.collect_context.skill import collect_context
        from cr_agent.skills.dimension_review.skill import dimension_review

        return SkillRegistry(
            collect_context=collect_context,
            dimension_review=dimension_review,
            summarize_report=self.summarize_report,
            validate_json=self.validate_json,
        )

    async def summarize_report(
        self,
        runtime_context,
        collected_context,
        dimension_scores,
        validation_errors,
    ):
        self.summarize_calls += 1
        return {"llm_result": f"# report {self.summarize_calls}"}

    async def validate_json(self, runtime_context, report: dict):
        from cr_agent.core.types import ValidationResult

        self.validate_calls += 1
        if self.validate_calls == 1:
            return ValidationResult(valid=False, errors=["retry once"])
        return ValidationResult(valid=True)


@pytest.mark.asyncio
async def test_dimension_usage_is_accumulated_once_across_summary_retry(
    agent_config_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dimensions_config = tmp_path / "dimensions.toml"
    _write_dimensions_config(
        dimensions_config,
        dimensions=["business", "security", "testing"],
        concurrency=3,
    )
    monkeypatch.setattr(dimension_skill, "_DIMENSIONS_CONFIG", dimensions_config)
    dimension_runtime = _DimensionRuntime(
        usage_by_dimension={
            "business": TokenUsage(input_tokens=1, output_tokens=10),
            "security": TokenUsage(input_tokens=2, output_tokens=20),
            "testing": TokenUsage(input_tokens=3, output_tokens=30),
        }
    )
    runtime_context = bootstrap_runtime(agent_config_path, platform_override="gitlab")
    runtime_context = replace(
        runtime_context,
        tool_facade=_FacadeForContextAndDimension(),
        context_runtime=_EmptyContextRuntime(),
        dimension_runtime=dimension_runtime,
    )
    scenario = _RetrySummaryScenario()
    fake_main_runtime = ScriptedMainAgentRuntime(
        usage=TokenUsage(input_tokens=100, output_tokens=200)
    )

    result = await run_review(
        runtime_context,
        agent_runtime=fake_main_runtime,
        skill_registry=scenario.registry(),
        max_retries=2,
    )

    assert result.status == "success"
    assert scenario.summarize_calls == 2
    assert scenario.validate_calls == 2
    assert result.tokens_consume.input_tokens == 106
    assert result.tokens_consume.output_tokens == 260
    assert load_review_result(runtime_context.result_dir / "result.json").status == "success"


class _ScoredFindingsRuntime:
    async def query_subagent(self, agent_name: str, prompt: str, *, assembled_options=None, timeout_s=None):
        dimension = _dimension_from_prompt(prompt)
        return QueryResult(
            text=(
                "review:\n"
                f"  dimension: {dimension}\n"
                "  score: 90\n"
                "  confidence: 80\n"
                "  findings:\n"
                "    - title: low\n"
                "      score: 55\n"
                "    - title: keep\n"
                "      score: 85\n"
            )
        )


@pytest.mark.asyncio
async def test_dimension_review_filters_findings_before_returning_to_summary(
    agent_config_path: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dimensions_config = tmp_path / "dimensions.toml"
    _write_dimensions_config(
        dimensions_config,
        dimensions=["security", "readability"],
        concurrency=2,
    )
    monkeypatch.setattr(dimension_skill, "_DIMENSIONS_CONFIG", dimensions_config)
    runtime_context = _runtime_context(
        agent_config_path,
        platform="gitlab",
        dimension_runtime=_ScoredFindingsRuntime(),
    )

    scores = await dimension_review(runtime_context, {"task_id": "task-1"})

    assert len(scores) == 2
    assert {finding["dimension"] for finding in scores} == {"security", "readability"}
    assert {finding["title"] for finding in scores} == {"keep"}

    security_artifact = _load_json(runtime_context.result_dir / "dimensions" / "security.json")
    assert [finding["title"] for finding in security_artifact["findings"]] == ["keep"]
