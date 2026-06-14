from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import toml

from cr_agent.bootstrap import RuntimeContext
from cr_agent.core.errors import RuntimeCallError
from cr_agent.core.types import QueryResult, TokenUsage
from cr_agent.tools.provider import ToolSpec
from cr_agent.tools.spec_sdk import to_sdk_tool
from cr_agent.utils.logging import get_logger

_logger = get_logger("cr_agent.skills.dimension_review")

_DIMENSIONS_CONFIG = Path(__file__).resolve().parents[3] / "config" / "dimensions.toml"
_PROMPT_DIR = Path(__file__).resolve().parents[3] / "prompt"


async def dimension_review(
    runtime_context: RuntimeContext,
    collected_context: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    并发执行维度审查。

    维度列表由 config/dimensions.toml 按 platform 确定。单个维度失败写入失败产物后继续;
    只有全部维度失败才抛错,让上层按既有兜底写 result.json/run.log。
    """
    selection = _load_dimensions(runtime_context.platform)
    artifact_dir = runtime_context.result_dir / "dimensions"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    tools = runtime_context.tool_facade.tools_for("dimension")
    effective_concurrency = min(selection.concurrency, len(selection.dimensions))
    _logger.info(
        "DIMENSION_CONCURRENCY configured=%s effective=%s total=%s",
        selection.configured_concurrency,
        effective_concurrency,
        len(selection.dimensions),
    )
    semaphore = asyncio.Semaphore(effective_concurrency)

    async def run_limited(dimension: str) -> dict[str, Any]:
        async with semaphore:
            _logger.info("DIMENSION_START dimension=%s", dimension)
            result = await _run_single_dimension(
                runtime_context=runtime_context,
                collected_context=collected_context,
                dimension=dimension,
                tools=tools,
                artifact_dir=artifact_dir,
            )
            _logger.info(
                "DIMENSION_END dimension=%s status=%s",
                dimension,
                result["status"],
            )
            return result

    # gather 按传入 task 顺序返回结果,manifest 与 summary 输入保持配置顺序。
    results = await asyncio.gather(
        *(run_limited(dimension) for dimension in selection.dimensions)
    )
    manifest_entries = [_manifest_entry(result) for result in results]
    successful = [
        _score_for_summary(result)
        for result in results
        if result["status"] == "success"
    ]

    manifest = _build_manifest(
        platform=runtime_context.platform,
        selection=selection,
        entries=manifest_entries,
    )
    _write_json(artifact_dir / "manifest.json", manifest)
    _logger.info("ARTIFACT_WRITE path=%s", artifact_dir / "manifest.json")

    if not successful:
        raise RuntimeCallError("all dimension reviews failed")
    return successful


@dataclass(frozen=True)
class DimensionSelection:
    dimensions: list[str]
    fallback_used: bool
    concurrency: int
    configured_concurrency: Any


async def _run_single_dimension(
    *,
    runtime_context: RuntimeContext,
    collected_context: dict[str, Any],
    dimension: str,
    tools: list[ToolSpec],
    artifact_dir: Path,
) -> dict[str, Any]:
    artifact_path = artifact_dir / f"{dimension}.json"
    prompt_path = _PROMPT_DIR / f"{dimension}.md"
    try:
        prompt_text = prompt_path.read_text(encoding="utf-8")
        prompt = _build_prompt(runtime_context, collected_context, dimension, prompt_text, tools)
        options = _build_dimension_options(runtime_context, tools)
        runtime = getattr(runtime_context, "dimension_runtime", None)
        if runtime is None:
            raise RuntimeCallError("dimension runtime is not configured")
        response: QueryResult = await runtime.query_subagent(
            "dimension",
            prompt,
            assembled_options=options,
        )
        parsed = _parse_dimension_text(response.text)
        result = _success_artifact(
            dimension=dimension,
            parsed=parsed,
            usage=response.usage,
            raw_text=response.text,
            artifact_path=artifact_path,
        )
    except Exception as exc:
        result = _failed_artifact(
            dimension=dimension,
            error=str(exc),
            artifact_path=artifact_path,
        )
        _logger.warning(
            "DEGRADED reason=DIMENSION_FAILED dimension=%s error=%s",
            dimension,
            exc,
        )

    _write_json(artifact_path, result)
    _logger.info("ARTIFACT_WRITE path=%s", artifact_path)
    return result


def _load_dimensions(platform: str) -> DimensionSelection:
    data = toml.load(_DIMENSIONS_CONFIG)
    concurrency = _concurrency_from(data)
    configured_concurrency = data.get("defaults", {}).get("concurrency", 1)
    profile_dimensions = _dimensions_from(data.get("profiles", {}).get(platform, {}))
    if profile_dimensions:
        return DimensionSelection(
            profile_dimensions,
            fallback_used=False,
            concurrency=concurrency,
            configured_concurrency=configured_concurrency,
        )

    fallback = _dimensions_from(data.get("defaults", {}).get("fallback", {}))
    if fallback:
        return DimensionSelection(
            fallback,
            fallback_used=True,
            concurrency=concurrency,
            configured_concurrency=configured_concurrency,
        )
    raise RuntimeCallError(f"no dimensions configured for platform={platform}")


def _concurrency_from(data: dict[str, Any]) -> int:
    raw = data.get("defaults", {}).get("concurrency", 1)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 1
    return max(1, value)


def _dimensions_from(section: Any) -> list[str]:
    if not isinstance(section, dict):
        return []
    raw = section.get("dimensions")
    if not isinstance(raw, list):
        return []
    dimensions: list[str] = []
    seen: set[str] = set()
    for item in raw:
        value = str(item).strip()
        if value and value not in seen:
            dimensions.append(value)
            seen.add(value)
    return dimensions


def _build_dimension_options(runtime_context: RuntimeContext, tools: list[ToolSpec]) -> Any:
    if not tools:
        return None
    from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server

    from cr_agent.core.sdk_runtime import build_sdk_env

    server = create_sdk_mcp_server(name="dimension", tools=[to_sdk_tool(t) for t in tools])
    options = ClaudeAgentOptions(
        model=runtime_context.config.llm.model,
        env=build_sdk_env(runtime_context.config.llm),
        mcp_servers={"dimension": server},
        allowed_tools=[f"mcp__dimension__{tool.name}" for tool in tools],
        tools=[],
    )
    options._cr_agent_tools = tools  # type: ignore[attr-defined]
    return options


def _build_prompt(
    runtime_context: RuntimeContext,
    collected_context: dict[str, Any],
    dimension: str,
    dimension_prompt: str,
    tools: list[ToolSpec],
) -> str:
    review_input = runtime_context.review_input
    payload = {
        "dimension": dimension,
        "task_id": review_input.task_id,
        "platform": runtime_context.platform,
        "title": review_input.title,
        "project_root": review_input.project_root,
        "collected_context": collected_context,
        "available_tools": [
            {"name": tool.name, "description": tool.description, "input_schema": tool.input_schema}
            for tool in tools
        ],
    }
    return (
        "You are the dimension subagent for a code review.\n"
        f"Review dimension: {dimension}.\n"
        "Follow the dimension-specific rules below, use available tools when useful, "
        "and return ONLY valid JSON matching this shape: "
        '{"dimension": string, "score": 0-100, "confidence": 0-100, "findings": []}.\n'
        "Dimension rules:\n"
        f"{dimension_prompt}\n\n"
        "Input:\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _parse_dimension_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise RuntimeCallError("dimension subagent returned empty output")
    json_text = _strip_code_fence(stripped)
    try:
        parsed = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise RuntimeCallError("dimension subagent returned non-json output") from exc
    if not isinstance(parsed, dict):
        raise RuntimeCallError("dimension subagent returned json that is not an object")
    return parsed


def _strip_code_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) >= 3 and lines[-1].strip() == "```":
        first = lines[0].strip().lower()
        if first in {"```", "```json"}:
            return "\n".join(lines[1:-1]).strip()
    return text


def _success_artifact(
    *,
    dimension: str,
    parsed: dict[str, Any],
    usage: TokenUsage,
    raw_text: str,
    artifact_path: Path,
) -> dict[str, Any]:
    score = _bounded_int(parsed.get("score"), default=0)
    confidence = _bounded_int(parsed.get("confidence"), default=0)
    findings = parsed.get("findings")
    warnings = parsed.get("warnings")
    return {
        "dimension": str(parsed.get("dimension") or dimension),
        "status": "success",
        "score": score,
        "confidence": confidence,
        "findings": findings if isinstance(findings, list) else [],
        "warnings": warnings if isinstance(warnings, list) else [],
        "error": None,
        "usage": _usage_dict(usage),
        "raw_text": raw_text,
        "artifact_path": str(artifact_path),
    }


def _failed_artifact(*, dimension: str, error: str, artifact_path: Path) -> dict[str, Any]:
    return {
        "dimension": dimension,
        "status": "failed",
        "score": 0,
        "confidence": 0,
        "findings": [],
        "warnings": [error],
        "error": error,
        "usage": _usage_dict(TokenUsage()),
        "raw_text": "",
        "artifact_path": str(artifact_path),
    }


def _bounded_int(value: Any, *, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(100, number))


def _manifest_entry(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "dimension": result["dimension"],
        "status": result["status"],
        "artifact_path": result["artifact_path"],
        "score": result["score"],
        "confidence": result["confidence"],
        "error": result["error"],
    }


def _score_for_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "dimension": result["dimension"],
        "score": result["score"],
        "confidence": result["confidence"],
        "findings": result["findings"],
        "warnings": result["warnings"],
        "artifact_path": result["artifact_path"],
        "usage": result["usage"],
    }


def _build_manifest(
    *,
    platform: str,
    selection: DimensionSelection,
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    succeeded = sum(1 for entry in entries if entry["status"] == "success")
    failed = len(entries) - succeeded
    if succeeded == len(entries):
        status = "success"
    elif succeeded == 0:
        status = "failed"
    else:
        status = "degraded"
    return {
        "platform": platform,
        "configured_dimensions": selection.dimensions,
        "fallback_used": selection.fallback_used,
        "total": len(entries),
        "succeeded": succeeded,
        "failed": failed,
        "status": status,
        "dimensions": entries,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _usage_dict(usage: TokenUsage) -> dict[str, int]:
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
    }
