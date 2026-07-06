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
from cr_agent.tools.provider import ToolResult, ToolSpec, error_result
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
    if collected_context.get("status") == "failed":
        raise RuntimeCallError(
            "dimension_review skipped because collect_context failed: "
            f"{collected_context.get('error') or 'unknown'}"
        )
    
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
    response: QueryResult | None = None
    try:
        prompt_text = prompt_path.read_text(encoding="utf-8")
        rule_text = rule_path.read_text(encoding="utf-8") if rule_path.exists() else ""
        # 每个维度单独包一层 tracing,日志里能区分是哪个维度发起的工具调用。
        budget_decision = _dimension_tool_budget_decision(runtime_context, collected_context)
        tool_budget_bytes = budget_decision["budget_bytes"]
        max_grep_calls = _positive_int(
            getattr(runtime_context.config.tools.dimension, "max_grep_calls", 40),
            default=40,
        )
        max_read_file_range_calls = _dimension_read_file_range_limit(
            runtime_context,
            dimension,
        )
        max_total_tool_calls = _positive_int(
            getattr(runtime_context.config.tools.dimension, "max_total_tool_calls", 80),
            default=80,
        )
        _logger.info(
            "DIMENSION_TOOL_BUDGET dimension=%s budget_bytes=%s changed_lines=%s "
            "diff_bytes=%s tier=%s max_grep_calls=%s max_read_file_range_calls=%s "
            "max_total_tool_calls=%s",
            dimension,
            tool_budget_bytes,
            budget_decision.get("changed_lines"),
            budget_decision.get("diff_bytes"),
            budget_decision.get("tier"),
            max_grep_calls,
            max_read_file_range_calls,
            max_total_tool_calls,
        )
        budgeted_tools = _wrap_dimension_tool_budget(
            tools,
            budget_bytes=tool_budget_bytes,
            max_grep_calls=max_grep_calls,
            max_read_file_range_calls=max_read_file_range_calls,
            max_total_tool_calls=max_total_tool_calls,
        )
        traced_tools = trace_tools(budgeted_tools, agent_name=f"dimension:{dimension}")
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
        response = await runtime.query_subagent(
            "dimension",
            prompt,
            assembled_options=options,
            timeout_s=runtime_context.config.timeouts.dimension_s,
        )
        raw_yaml = response.text
        parsed = _parse_dimension_yaml(raw_yaml)
        result = _success_artifact(
            runtime_context=runtime_context,
            dimension=dimension,
            parsed=parsed,
            usage=response.usage,
            raw_yaml=response.text,
            artifact_path=artifact_path,
        )
    except Exception as exc:
        failure_usage = _usage_from_failure(exc, response)
        result = _failed_artifact(
            dimension=dimension,
            error=str(exc),
            raw_yaml=raw_yaml,
            usage=failure_usage,
            error_kind=str(getattr(exc, "error_kind", "") or ""),
            raw_error_result=str(getattr(exc, "raw_error_result", "") or ""),
            error_diagnostics=getattr(exc, "diagnostics", None),
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
    tool_limits = _dimension_prompt_tool_limits(runtime_context, dimension)
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
        "硬性执行限制：\n"
        "- 一旦已有足够证据支持输出 findings 或空 findings，立即停止探索并输出 YAML。\n"
        "- 不要在 YAML 前输出审查过程、已检查文件清单、关键观察或思考说明。\n"
        "- 每次工具调用前判断是否会改变结论；如果不会改变结论，不要调用工具。\n"
        f"- 本维度最多调用 read_file_range {tool_limits['max_read_file_range_calls']} 次、"
        f"grep_text {tool_limits['max_grep_calls']} 次、工具总调用 {tool_limits['max_total_tool_calls']} 次；"
        "接近上限时必须停止调用工具并输出当前 YAML。\n"
        "- 如果连续两次工具调用没有得到新的 diff 内可定位证据，必须停止探索并输出 YAML。\n\n"
        "以下是本次运行输入：\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "请严格按照上方 SKILL.md、维度提示词和维度评分规则执行，并返回指定 YAML 输出。"
    )


def _dimension_prompt_tool_limits(
    runtime_context: RuntimeContext,
    dimension: str,
) -> dict[str, int]:
    return {
        "max_grep_calls": _positive_int(
            getattr(runtime_context.config.tools.dimension, "max_grep_calls", 40),
            default=40,
        ),
        "max_read_file_range_calls": _dimension_read_file_range_limit(
            runtime_context,
            dimension,
        ),
        "max_total_tool_calls": _positive_int(
            getattr(runtime_context.config.tools.dimension, "max_total_tool_calls", 80),
            default=80,
        ),
    }


def _dimension_tool_budget_bytes(
    runtime_context: RuntimeContext,
    collected_context: dict[str, Any],
) -> int:
    return _dimension_tool_budget_decision(runtime_context, collected_context)["budget_bytes"]


def _dimension_read_file_range_limit(
    runtime_context: RuntimeContext,
    dimension: str,
) -> int:
    config = runtime_context.config.tools.dimension
    overrides = getattr(config, "dimension_max_read_file_range_calls", {}) or {}
    if isinstance(overrides, dict) and dimension in overrides:
        return _positive_int(overrides.get(dimension), default=40)
    return _positive_int(getattr(config, "max_read_file_range_calls", 40), default=40)


def _dimension_tool_budget_decision(
    runtime_context: RuntimeContext,
    collected_context: dict[str, Any],
) -> dict[str, Any]:
    config = runtime_context.config.tools.dimension
    changed_lines = _changed_lines_count(collected_context)
    if changed_lines is not None:
        if changed_lines <= _positive_int(config.small_changed_lines, default=300):
            return {
                "budget_bytes": _positive_int(config.small_content_budget_bytes, default=20 * 1024),
                "changed_lines": changed_lines,
                "diff_bytes": None,
                "tier": "small_changed_lines",
            }
        if changed_lines > _positive_int(config.large_changed_lines, default=1200):
            return {
                "budget_bytes": _positive_int(config.large_content_budget_bytes, default=120 * 1024),
                "changed_lines": changed_lines,
                "diff_bytes": None,
                "tier": "large_changed_lines",
            }
        return {
            "budget_bytes": _positive_int(config.medium_content_budget_bytes, default=80 * 1024),
            "changed_lines": changed_lines,
            "diff_bytes": None,
            "tier": "medium_changed_lines",
        }

    diff_bytes = _raw_diff_bytes(collected_context)
    if diff_bytes <= _positive_int(config.small_diff_bytes, default=30 * 1024):
        return {
            "budget_bytes": _positive_int(config.small_content_budget_bytes, default=20 * 1024),
            "changed_lines": None,
            "diff_bytes": diff_bytes,
            "tier": "small_diff_bytes",
        }
    if diff_bytes > _positive_int(config.large_diff_bytes, default=80 * 1024):
        return {
            "budget_bytes": _positive_int(config.large_content_budget_bytes, default=120 * 1024),
            "changed_lines": None,
            "diff_bytes": diff_bytes,
            "tier": "large_diff_bytes",
        }
    return {
        "budget_bytes": _positive_int(config.medium_content_budget_bytes, default=80 * 1024),
        "changed_lines": None,
        "diff_bytes": diff_bytes,
        "tier": "medium_diff_bytes",
    }


def _changed_lines_count(collected_context: dict[str, Any]) -> int | None:
    changed_files = collected_context.get("changed_files")
    if not isinstance(changed_files, list):
        return None

    total = 0
    saw_lines = False
    for item in changed_files:
        if not isinstance(item, dict):
            continue
        for key in ("added_lines", "deleted_lines", "removed_lines", "modified_lines"):
            value = item.get(key)
            if isinstance(value, list):
                total += len(value)
                saw_lines = True
            elif isinstance(value, int):
                total += max(value, 0)
                saw_lines = True
        for key in ("added_ranges", "deleted_ranges", "removed_ranges", "modified_ranges"):
            value = item.get(key)
            if not isinstance(value, list):
                continue
            for raw_range in value:
                if not isinstance(raw_range, list) or len(raw_range) != 2:
                    continue
                try:
                    start = int(raw_range[0])
                    end = int(raw_range[1])
                except (TypeError, ValueError):
                    continue
                if end >= start:
                    total += end - start + 1
                    saw_lines = True
    return total if saw_lines else None


def _raw_diff_bytes(collected_context: dict[str, Any]) -> int:
    raw_diff = collected_context.get("raw_diff")
    if isinstance(raw_diff, str):
        return len(raw_diff.encode("utf-8"))
    if isinstance(raw_diff, dict):
        for key in ("content", "diff", "text", "patch"):
            value = raw_diff.get(key)
            if isinstance(value, str):
                return len(value.encode("utf-8"))
        return len(json.dumps(raw_diff, ensure_ascii=False).encode("utf-8"))
    return 0


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


def _wrap_dimension_tool_budget(
    tools: list[ToolSpec],
    *,
    budget_bytes: int,
    max_grep_calls: int = 40,
    max_read_file_range_calls: int = 40,
    max_total_tool_calls: int = 80,
) -> list[ToolSpec]:
    remaining = {"bytes": budget_bytes}
    grep_calls = {"count": 0}
    read_file_range_calls = {"count": 0}
    total_tool_calls = {"count": 0}
    wrapped: list[ToolSpec] = []
    for tool in tools:
        original_handler = tool.handler

        async def budgeted_handler(
            args: dict[str, Any],
            *,
            _tool: ToolSpec = tool,
            _handler=original_handler,
        ) -> ToolResult:
            total_tool_calls["count"] += 1
            if total_tool_calls["count"] > max_total_tool_calls:
                return error_result(
                    "dimension total tool call budget exhausted",
                    [
                        "dimension total tool call budget exhausted "
                        f"({max_total_tool_calls} calls per dimension)"
                    ],
                )
            if _tool.name == "grep_text":
                grep_calls["count"] += 1
                if grep_calls["count"] > max_grep_calls:
                    return error_result(
                        "dimension grep_text call budget exhausted",
                        [
                            "dimension grep_text call budget exhausted "
                            f"({max_grep_calls} calls per dimension)"
                        ],
                    )
            if _tool.name == "read_file_range":
                read_file_range_calls["count"] += 1
                if read_file_range_calls["count"] > max_read_file_range_calls:
                    return error_result(
                        "dimension read_file_range call budget exhausted",
                        [
                            "dimension read_file_range call budget exhausted "
                            f"({max_read_file_range_calls} calls per dimension)"
                        ],
                    )

            result: ToolResult = await _handler(args)
            data = result.get("data")
            if not result.get("ok") or not isinstance(data, dict) or "content" not in data:
                return result

            content = "" if data.get("content") is None else str(data.get("content"))
            if remaining["bytes"] <= 0:
                return error_result(
                    "dimension tool content budget exhausted",
                    [
                        "dimension tool content budget exhausted "
                        f"({budget_bytes} bytes per dimension)"
                    ],
                )

            content_bytes = len(content.encode("utf-8"))
            if content_bytes <= remaining["bytes"]:
                remaining["bytes"] -= content_bytes
                return result

            allowed = remaining["bytes"]
            remaining["bytes"] = 0
            warnings = list(result.get("warnings") or [])
            warnings.append(
                "dimension tool content budget truncated result to remaining "
                f"{allowed} bytes (budget={budget_bytes} bytes per dimension)"
            )
            if allowed <= 0:
                return error_result("dimension tool content budget exhausted", warnings)

            truncated = content.encode("utf-8")[:allowed].decode("utf-8", errors="ignore")
            new_data = dict(data)
            new_data["content"] = truncated
            new_data["content_budget_truncated"] = True
            return {
                "ok": True,
                "data": new_data,
                "warnings": warnings,
                "error": result.get("error"),
            }

        wrapped.append(
            ToolSpec(
                name=tool.name,
                description=tool.description,
                input_schema=tool.input_schema,
                handler=budgeted_handler,
            )
        )
    return wrapped


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
    runtime_context: RuntimeContext,
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
    normalized_findings = _limit_output_findings(
        normalized_findings,
        max_findings=_positive_int(
            runtime_context.config.tools.dimension.max_output_findings,
            default=3,
        ),
        max_field_chars=_positive_int(
            runtime_context.config.tools.dimension.max_field_chars,
            default=800,
        ),
    )
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


_FINDING_TEXT_FIELDS = ("title", "analysis", "evidence", "severity_hint", "suggestion")


def _limit_output_findings(
    findings: list[dict[str, Any]],
    *,
    max_findings: int,
    max_field_chars: int,
) -> list[dict[str, Any]]:
    limited = [_limit_finding_fields(finding, max_field_chars) for finding in findings]
    if max_findings <= 0 or len(limited) <= max_findings:
        return limited
    critical = [
        finding for finding in limited if _finding_is_exceptionally_severe(finding)
    ]
    if len(critical) >= max_findings:
        return critical
    others = [
        finding for finding in sorted(
            limited,
            key=lambda item: _positive_int(item.get("score"), default=0),
            reverse=True,
        )
        if finding not in critical
    ]
    return critical + others[: max_findings - len(critical)]


def _limit_finding_fields(finding: dict[str, Any], max_field_chars: int) -> dict[str, Any]:
    if max_field_chars <= 0:
        return finding
    limited = dict(finding)
    for key in _FINDING_TEXT_FIELDS:
        value = limited.get(key)
        if isinstance(value, str):
            limited[key] = _limit_chars(value, max_field_chars)
    raw = limited.get("raw")
    if isinstance(raw, dict):
        limited["raw"] = {
            key: _limit_chars(value, max_field_chars) if isinstance(value, str) else value
            for key, value in raw.items()
        }
    return limited


def _finding_is_exceptionally_severe(finding: dict[str, Any]) -> bool:
    if _positive_int(finding.get("score"), default=0) >= 90:
        return True
    text = " ".join(
        str(finding.get(key) or "").lower()
        for key in ("title", "analysis", "severity_hint")
    )
    return any(
        marker in text
        for marker in ("critical", "security", "data-loss", "data loss", "merge-blocking")
    )


def _limit_chars(text: str, max_chars: int) -> str:
    stripped = text.strip()
    if len(stripped) <= max_chars:
        return stripped
    return stripped[:max_chars].rstrip() + "...[truncated]"


def _failed_artifact(
    *,
    dimension: str,
    error: str,
    raw_yaml: str,
    usage: TokenUsage | None = None,
    error_kind: str = "",
    raw_error_result: str = "",
    error_diagnostics: Any = None,
    artifact_path: Path,
) -> dict[str, Any]:
    return {
        "dimension": dimension,
        "status": "failed",
        "score": 0,
        "confidence": 0,
        "findings": [],
        "warnings": [error],
        "error": error,
        "error_kind": error_kind,
        "raw_error_result": _truncate(raw_error_result, 1200),
        "error_diagnostics": error_diagnostics if isinstance(error_diagnostics, dict) else {},
        "usage": usage_to_dict(usage or TokenUsage()),
        "raw_yaml": _truncate(raw_yaml),
        "artifact_path": str(artifact_path),
    }


def _usage_from_failure(exc: Exception, response: QueryResult | None) -> TokenUsage:
    if response is not None:
        return response.usage
    raw_usage = getattr(exc, "usage", None)
    if isinstance(raw_usage, TokenUsage):
        return raw_usage
    if isinstance(raw_usage, dict):
        return TokenUsage(
            input_tokens=_positive_int(raw_usage.get("input_tokens"), default=0),
            output_tokens=_positive_int(raw_usage.get("output_tokens"), default=0),
            cache_creation_tokens=_positive_int(
                raw_usage.get("cache_creation_tokens")
                or raw_usage.get("cache_creation_input_tokens"),
                default=0,
            ),
            cache_read_tokens=_positive_int(
                raw_usage.get("cache_read_tokens") or raw_usage.get("cache_read_input_tokens"),
                default=0,
            ),
            cost=float(raw_usage.get("cost") or raw_usage.get("total_cost_usd") or 0.0),
        )
    return TokenUsage()


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
