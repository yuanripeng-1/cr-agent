from __future__ import annotations

import asyncio
import json
import re
import time
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
from cr_agent.core.finding_filter import filter_dimension_result, flatten_filtered_findings
from cr_agent.core.review_timing import record_dimension_agent_s, record_dimension_skill_s
from cr_agent.core.types import QueryResult, TokenUsage
from cr_agent.core.usage import usage_to_dict
from cr_agent.skills.docs import load_skill_doc
from cr_agent.skills.tool_trace import trace_tools
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
    skill_start = time.monotonic()
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
            dim_start = time.monotonic()
            _logger.info("DIMENSION_START dimension=%s", dimension)
            result = await _run_single_dimension(
                runtime_context=runtime_context,
                collected_context=collected_context,
                dimension=dimension,
                tools=tools,
                artifact_dir=artifact_dir,
            )
            dim_elapsed = time.monotonic() - dim_start
            record_dimension_agent_s(dimension, dim_elapsed)
            _logger.info(
                "AGENT_TIMING agent=dimension dimension=%s elapsed_s=%.2f",
                dimension,
                dim_elapsed,
            )
            _logger.info(
                "DIMENSION_END dimension=%s status=%s elapsed_s=%.2f",
                dimension,
                result["status"],
                dim_elapsed,
            )
            return result

    # gather 按传入 task 顺序返回结果,manifest 与 summary 输入保持配置顺序。
    results = await asyncio.gather(
        *(run_limited(dimension) for dimension in selection.dimensions)
    )
    persisted_results: list[dict[str, Any]] = []
    for result in results:
        if result["status"] == "success":
            filtered = filter_dimension_result(result)
            _write_json(Path(result["artifact_path"]), filtered)
            _logger.info("ARTIFACT_WRITE path=%s filtered=true", result["artifact_path"])
            persisted_results.append(filtered)
        else:
            persisted_results.append(result)

    manifest_entries = [_manifest_entry(result) for result in persisted_results]

    manifest = _build_manifest(
        platform=runtime_context.platform,
        selection=selection,
        entries=manifest_entries,
    )
    _write_json(artifact_dir / "manifest.json", manifest)
    _logger.info("ARTIFACT_WRITE path=%s", artifact_dir / "manifest.json")

    successful_findings = flatten_filtered_findings(persisted_results)
    if not any(result.get("status") == "success" for result in persisted_results):
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
    skill_elapsed = time.monotonic() - skill_start
    record_dimension_skill_s(skill_elapsed)
    _logger.info("SKILL_TIMING skill=dimension_review elapsed_s=%.2f", skill_elapsed)
    return successful_findings


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
        # 每个维度单独包一层 tracing,日志里能区分是哪个维度发起的工具调用。
        traced_tools = trace_tools(tools, agent_name=f"dimension:{dimension}")
        prompt = _build_prompt(
            runtime_context,
            collected_context,
            dimension,
            prompt_text,
            rule_text,
            traced_tools,
        )
        options = _build_dimension_options(runtime_context, traced_tools)
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
    }
    return (
        f"{skill_doc}\n\n"
        f"当前评审维度：{dimension}\n\n"
        "维度提示词：\n"
        f"{dimension_prompt}\n\n"
        "维度评分规则：\n"
        f"{dimension_rule}\n\n"
        "以下是本次运行输入：\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "请严格按照上方 SKILL.md、维度提示词和维度评分规则执行，并返回指定 YAML 输出。"
    )


def _parse_dimension_yaml(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise RuntimeCallError("dimension subagent returned empty output")
    yaml_text = _isolate_yaml(stripped)

    # yaml 缺失时退回极简解析器(只处理纯净 mapping)。
    if yaml is None:
        parsed = _minimal_yaml_load(yaml_text)
        if not isinstance(parsed, dict):
            raise RuntimeCallError("dimension subagent returned yaml that is not an object")
        return parsed

    # 模型常在 YAML 主体后追加散文/说明,导致整体 safe_load 失败。
    # 先整体解析;失败再从尾部逐行回退,取“仍能解析成 mapping 的最长前缀”。
    parsed = _try_load_yaml(yaml_text)
    if isinstance(parsed, dict):
        return parsed

    lines = yaml_text.splitlines()
    for end in range(len(lines) - 1, 0, -1):
        candidate = "\n".join(lines[:end]).rstrip()
        if not candidate:
            continue
        parsed = _try_load_yaml(candidate)
        if isinstance(parsed, dict):
            return parsed

    raise RuntimeCallError("dimension subagent returned invalid yaml")


def _try_load_yaml(text: str) -> Any:
    """安全 safe_load:任何 YAML 错误都吞掉返回 None,交由调用方回退。"""
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    except Exception:  # noqa: BLE001 - 个别畸形输入会触发非 YAMLError。
        return None


# YAML 主体的起始锚点:覆盖各维度 prompt 产出的顶层键(见 _normalize_findings)。
_YAML_ANCHORS = (
    "review",
    "dimension",
    "score",
    "findings",
    "vulnerabilities",
    "issues",
    "analysis",
    "suggestions",
    "bottlenecks",
    "violations",
)


def _isolate_yaml(text: str) -> str:
    """
    从模型输出里抠出 YAML 主体。

    1. 若存在 ```yaml/```yml/``` 围栏,取第一个围栏块内容(忽略围栏前后的散文);
    2. 否则从首个锚点行起取到末尾(尾部多余散文由 _parse_dimension_yaml 的回退裁掉)。
    """
    fence = re.search(r"```(?:ya?ml)?[ \t]*\n(.*?)\n[ \t]*```", text, re.DOTALL)
    if fence:
        return fence.group(1).strip()

    lines = text.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        for anchor in _YAML_ANCHORS:
            if stripped == f"{anchor}:" or stripped.startswith(f"{anchor}:"):
                return "\n".join(lines[index:]).strip()
    return text


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
        "usage": usage_to_dict(usage),
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
        "usage": usage_to_dict(TokenUsage()),
        "raw_yaml": _truncate(raw_yaml),
        "artifact_path": str(artifact_path),
    }


def _bounded_int(value: Any, *, default: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(100, number))


def _positive_int(value: Any, *, default: int = 0) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, number)


def _manifest_entry(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "dimension": result["dimension"],
        "status": result["status"],
        "artifact_path": result["artifact_path"],
        "score": result["score"],
        "confidence": result["confidence"],
        "error": result["error"],
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
                "start_line": _positive_int(item.get("start_line"), default=0),
                "end_line": _positive_int(item.get("end_line"), default=0),
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
