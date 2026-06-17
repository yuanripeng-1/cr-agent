from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import toml
try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - exercised when dependency is absent.
    yaml = None  # type: ignore[assignment]

from cr_agent.bootstrap import RuntimeContext
from cr_agent.core.errors import RuntimeCallError
from cr_agent.core.types import QueryResult, TokenUsage
from cr_agent.skills.docs import load_skill_doc
from cr_agent.tools.provider import ToolSpec
from cr_agent.tools.spec_sdk import to_sdk_tool
from cr_agent.utils.logging import get_logger

_logger = get_logger("cr_agent.skills.dimension_review")

_DIMENSIONS_CONFIG = Path(__file__).resolve().parents[3] / "config" / "dimensions.toml"
_PROMPT_DIR = Path(__file__).resolve().parents[3] / "prompt"
_RULES_DIR = _PROMPT_DIR / "rules"


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
        errors = sorted({str(entry.get("error") or "unknown") for entry in manifest_entries})
        _logger.error(
            "DIMENSION_ALL_FAILED total=%s manifest=%s errors=%s",
            len(manifest_entries),
            artifact_dir / "manifest.json",
            errors,
        )
        raise RuntimeCallError(
            "all dimension reviews failed; "
            f"manifest={artifact_dir / 'manifest.json'}; "
            f"errors={errors}"
        )
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
    rule_path = _RULES_DIR / f"{_rule_file_stem(dimension)}Rule.md"
    raw_yaml = ""
    try:
        prompt_text = prompt_path.read_text(encoding="utf-8")
        rule_text = rule_path.read_text(encoding="utf-8") if rule_path.exists() else ""
        prompt = _build_prompt(
            runtime_context,
            collected_context,
            dimension,
            prompt_text,
            rule_text,
            tools,
        )
        options = _build_dimension_options(runtime_context, tools)
        runtime = getattr(runtime_context, "dimension_runtime", None)
        if runtime is None:
            raise RuntimeCallError("dimension runtime is not configured")
        # Dimension 子 Agent 启动
        response: QueryResult = await runtime.query_subagent(
            "dimension",
            prompt,
            assembled_options=options,
            timeout_s=runtime_context.config.timeouts.dimension_s,
        )
        raw_yaml = response.text
        parsed = _parse_dimension_yaml(raw_yaml)
        result = _success_artifact(
            dimension=dimension,
            parsed=parsed,
            usage=response.usage,
            raw_yaml=response.text,
            artifact_path=artifact_path,
        )
    except Exception as exc:
        result = _failed_artifact(
            dimension=dimension,
            error=str(exc),
            raw_yaml=raw_yaml,
            artifact_path=artifact_path,
        )
        _logger.warning(
            "DEGRADED reason=DIMENSION_FAILED dimension=%s error=%s raw_preview=%s",
            dimension,
            exc,
            _truncate(raw_yaml, 1200),
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
    dimension_rule: str,
    tools: list[ToolSpec],
) -> str:
    review_input = runtime_context.review_input
    skill_doc = load_skill_doc("dimension_review")
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
        f"{skill_doc}\n\n"
        "你是代码评审的 dimension subagent。\n"
        f"评审维度：{dimension}。\n"
        "请遵循下面的维度专属规则，在有帮助时使用可用工具，"
        "并且只返回有效 YAML。不要包含 Markdown 代码围栏。\n"
        "维度提示词：\n"
        f"{dimension_prompt}\n\n"
        "维度评分规则：\n"
        f"{dimension_rule}\n\n"
        "输入：\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}"
    )


def _parse_dimension_yaml(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise RuntimeCallError("dimension subagent returned empty output")
    yaml_text = _extract_yaml_text(stripped)
    parsed = _safe_load_yaml(yaml_text)
    if not isinstance(parsed, dict):
        raise RuntimeCallError("dimension subagent returned yaml that is not an object")
    return parsed


def _safe_load_yaml(text: str) -> Any:
    if yaml is not None:
        try:
            return yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise RuntimeCallError("dimension subagent returned invalid yaml") from exc
    return _minimal_yaml_load(text)


def _minimal_yaml_load(text: str) -> dict[str, Any]:
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw_line in text.splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        line = raw_line.strip()
        if line.startswith("- "):
            raise RuntimeCallError("dimension subagent returned yaml that is not an object")
        if ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        while stack and indent <= stack[-1][0]:
            stack.pop()
        current = stack[-1][1]
        if value == "":
            child: dict[str, Any] = {}
            current[key] = child
            stack.append((indent, child))
        else:
            current[key] = _minimal_yaml_scalar(value)
    return root


def _minimal_yaml_scalar(value: str) -> Any:
    if value in {"[]", "null", "None"}:
        return [] if value == "[]" else None
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    try:
        return int(value)
    except ValueError:
        return value.strip("\"'")


def _extract_yaml_text(text: str) -> str:
    fenced = _strip_code_fence(text)
    if fenced != text:
        return fenced
    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped in {"review:", "dimension:", "score:", "findings:", "vulnerabilities:", "issues:"}:
            return "\n".join(lines[index:]).strip()
        if stripped.startswith(("review:", "dimension:", "score:", "findings:", "vulnerabilities:", "issues:")):
            return "\n".join(lines[index:]).strip()
    return text


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
    raw_yaml: str,
    artifact_path: Path,
) -> dict[str, Any]:
    review = parsed.get("review") if isinstance(parsed.get("review"), dict) else parsed
    score = _bounded_int(review.get("score"), default=0)
    confidence = _bounded_int(review.get("confidence"), default=0)
    warnings = review.get("warnings")
    normalized_findings = _normalize_findings(dimension, review)
    return {
        "dimension": str(review.get("dimension") or dimension),
        "status": "success",
        "score": score,
        "confidence": confidence,
        "findings": normalized_findings,
        "normalized_findings": normalized_findings,
        "warnings": warnings if isinstance(warnings, list) else [],
        "error": None,
        "usage": _usage_dict(usage),
        "raw_yaml": raw_yaml,
        "artifact_path": str(artifact_path),
    }


def _failed_artifact(*, dimension: str, error: str, raw_yaml: str, artifact_path: Path) -> dict[str, Any]:
    return {
        "dimension": dimension,
        "status": "failed",
        "score": 0,
        "confidence": 0,
        "findings": [],
        "warnings": [error],
        "error": error,
        "usage": _usage_dict(TokenUsage()),
        "raw_yaml": _truncate(raw_yaml),
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
        "normalized_findings": result.get("normalized_findings", result["findings"]),
        "raw_yaml": result.get("raw_yaml", ""),
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


def _normalize_findings(dimension: str, report: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: list[Any] = []
    for key in ("findings", "vulnerabilities", "issues", "analysis", "suggestions", "bottlenecks", "violations"):
        value = report.get(key)
        if isinstance(value, list):
            candidates.extend(value)
    normalized: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        title = (
            item.get("title")
            or item.get("requirement_name")
            or item.get("category")
            or item.get("type")
            or item.get("issue")
            or item.get("description")
            or f"{dimension} finding"
        )
        normalized.append(
            {
                "dimension": dimension,
                "title": _text(title),
                "analysis": _text(
                    item.get("analysis")
                    or item.get("description")
                    or item.get("risk")
                    or item.get("issue")
                    or ""
                ),
                "evidence": _text(item.get("evidence") or item.get("existing_code") or ""),
                "severity_hint": _text(item.get("severity") or item.get("category") or ""),
                "file_path": _text(item.get("file_path") or item.get("path") or ""),
                "start_line": _bounded_int(item.get("start_line"), default=0),
                "end_line": _bounded_int(item.get("end_line"), default=0),
                "suggestion": _text(
                    item.get("suggestion")
                    or item.get("mitigation")
                    or item.get("code_suggestion")
                    or ""
                ),
                "score": _bounded_int(item.get("score"), default=_bounded_int(report.get("score"), default=0)),
                "raw": item,
            }
        )
    return normalized


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict) or isinstance(value, list):
        return json.dumps(value, ensure_ascii=False)
    return "" if value is None else str(value)


def _rule_file_stem(dimension: str) -> str:
    mapping = {
        "error_handling": "errorHandling",
    }
    return mapping.get(dimension, dimension)


def _truncate(text: str, limit: int = 8000) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]"
