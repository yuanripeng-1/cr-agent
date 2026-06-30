from __future__ import annotations

import json
import hashlib
import time
from pathlib import Path
from typing import Any

from cr_agent.bootstrap import RuntimeContext
from cr_agent.core.review_timing import record_context_agent_s
from cr_agent.core.types import QueryResult
from cr_agent.core.usage import usage_to_dict
from cr_agent.skills.docs import load_skill_doc
from cr_agent.skills.tool_trace import trace_tools
from cr_agent.tools.provider import ToolResult, ToolSpec
from cr_agent.tools.spec_sdk import to_sdk_tool
from cr_agent.utils.diff import changed_file_payload
from cr_agent.utils.logging import get_logger

_logger = get_logger("cr_agent.skills.collect_context")
_MAX_CONTEXT_ENTRIES = 8
_MAX_CONTEXT_TEXT = 1200
_MAX_SNIPPETS = 12
_MAX_SNIPPET_TEXT = 1600
_MAX_EVIDENCE_EXCERPT = 500
_FALLBACK_FULL_DIFF_BYTES = 30 * 1024
_FALLBACK_SNIPPET_LINES = 6


async def collect_context(runtime_context: RuntimeContext) -> dict[str, Any]:
    """
    启动 context subagent 收集上下文。

    工具只从 facade.tools_for("context") 获取。Python 不预判 Semble/CRG query,
    由 context subagent 根据 diff 和前序工具结果动态决定是否调用。
    """
    review_input = runtime_context.review_input
    artifact_path = runtime_context.result_dir / "collected_context.json"
    evidence: list[dict[str, Any]] = []


    '''
        取出 collect_context 需要的工具,用公共 trace_tools 包一层:
        统一打 agent=context 的工具调用日志,并通过 on_call 把每次调用记进 evidence。
    '''
    tools = trace_tools(
        runtime_context.tool_facade.tools_for("context"),
        agent_name="context",
        on_call=lambda tool_name, args, result: evidence.append(
            _evidence_entry(tool_name, args, result)
        ),
    )
    prompt = _build_prompt(runtime_context, tools)
    options = _build_context_options(runtime_context, tools)
    runtime = getattr(runtime_context, "context_runtime", None)
    if runtime is None:
        raise RuntimeError("context runtime is not configured")

    try:
        # Context 子 Agent 启动
        agent_start = time.monotonic()
        result = await runtime.query_subagent(
            "context",
            prompt,
            assembled_options=options,
            timeout_s=runtime_context.config.timeouts.context_s,
        )
        agent_elapsed = time.monotonic() - agent_start
        record_context_agent_s(agent_elapsed)
        _logger.info("AGENT_TIMING agent=context elapsed_s=%.2f", agent_elapsed)
        degraded_warning = ""
    except Exception as exc:
        _logger.warning("DEGRADED reason=CONTEXT_SUBAGENT_FAILED error=%s", exc)
        result = QueryResult(text="")
        try:
            report = _build_fallback_report(runtime_context, str(exc))
        except Exception as fallback_exc:
            _logger.warning(
                "DEGRADED reason=CONTEXT_FALLBACK_FAILED original_error=%s fallback_error=%s",
                exc,
                fallback_exc,
            )
            report = _fatal_fallback_report(
                original_error=str(exc),
                fallback_error=str(fallback_exc),
            )
        if report.get("fatal"):
            artifact = _base_artifact(
                runtime_context=runtime_context,
                report=report,
                evidence=evidence,
                warnings=report.get("warnings") or [],
            )
            _write_json(artifact_path, artifact)
            _logger.info("ARTIFACT_WRITE path=%s", artifact_path)
            return {
                "task_id": review_input.task_id,
                "artifact_path": str(artifact_path),
                "summary": report.get("summary", ""),
                "diff_summary": report.get("diff_summary", ""),
                "changed_files": [],
                "semantic_context": [],
                "call_graph_context": [],
                "code_snippets": [],
                "warnings": report.get("warnings") or [],
                "status": "failed",
                "error": report.get("error") or "context fallback failed",
                "usage": usage_to_dict(result.usage),
            }
        degraded_warning = f"context subagent 失败: {exc}"
    else:
        report = _parse_context_text(result.text)
    warnings = _collect_warnings(evidence, report)
    if degraded_warning and degraded_warning not in warnings:
        warnings.append(degraded_warning)
    artifact = _base_artifact(
        runtime_context=runtime_context,
        report=report,
        evidence=evidence,
        warnings=warnings,
    )
    _write_json(artifact_path, artifact)
    _logger.info("ARTIFACT_WRITE path=%s", artifact_path)
    return {
        "task_id": review_input.task_id,
        "artifact_path": str(artifact_path),
        "summary": artifact["summary"] or artifact["diff_summary"],
        "diff_summary": artifact["diff_summary"],
        "changed_files": _compact_changed_files(artifact["changed_files"]),
        "semantic_context": _compact_context_entries(artifact["semantic_context"]),
        "call_graph_context": _compact_context_entries(artifact["call_graph_context"]),
        "code_snippets": _compact_code_snippets(artifact["code_snippets"]),
        "warnings": warnings,
        "usage": usage_to_dict(result.usage),
    }


def _base_artifact(
    *,
    runtime_context: RuntimeContext,
    report: dict[str, Any],
    evidence: list[dict[str, Any]],
    warnings: list[str],
) -> dict[str, Any]:
    review_input = runtime_context.review_input
    diff_content = str(report.get("_diff_content") or review_input.diff_content)
    artifact = {
        "task_id": review_input.task_id,
        "title": review_input.title,
        "description": review_input.description,
        "commit_messages": review_input.commit_messages,
        "requirements_doc": review_input.requirements_doc,
        "previous_report": review_input.previous_report,
        "project_root": review_input.project_root,
        "platform": runtime_context.platform,
        "raw_diff": {
            "source": report.get("_diff_source") or "context.json.diff_content",
            "content": diff_content,
            "diff_file_path": review_input.diff_file_path,
        },
        "changed_files": changed_file_payload(diff_content),
        "diff_summary": report.get("diff_summary", ""),
        "summary": report.get("summary", ""),
        "semantic_context": _section(report, "semantic_context", "semble_search", evidence),
        "call_graph_context": _section(report, "call_graph_context", "crg_", evidence),
        "code_snippets": _code_snippets(report, evidence),
        "warnings": warnings,
    }
    if runtime_context.config.debug.full_tool_evidence:
        artifact["tool_evidence"] = evidence
    else:
        artifact["tool_evidence_summary"] = [
            _evidence_summary(entry) for entry in evidence
        ]
    if "diff_excerpt" in report:
        artifact["diff_excerpt"] = report["diff_excerpt"]
    if "changed_line_snippets" in report:
        artifact["changed_line_snippets"] = report["changed_line_snippets"]
    if report.get("fatal"):
        artifact["status"] = "failed"
        artifact["error"] = report.get("error")
    return artifact


def _build_context_options(runtime_context: RuntimeContext, tools: list[ToolSpec]) -> Any:
    if not tools:
        return None
    from claude_agent_sdk import ClaudeAgentOptions, create_sdk_mcp_server

    from cr_agent.core.sdk_runtime import build_sdk_env

    server = create_sdk_mcp_server(name="context", tools=[to_sdk_tool(t) for t in tools])
    options = ClaudeAgentOptions(
        model=runtime_context.config.llm.model,
        env=build_sdk_env(runtime_context.config.llm),
        mcp_servers={"context": server},
        allowed_tools=[f"mcp__context__{tool.name}" for tool in tools],
        tools=[],
    )
    # 供测试 fake runtime 与本地 instrumentation 读取;真实 SDK 忽略这个动态属性。
    options._cr_agent_tools = tools  # type: ignore[attr-defined]
    return options

# 构建context subagent的prompt
def _build_prompt(runtime_context: RuntimeContext, tools: list[ToolSpec]) -> str:
    review_input = runtime_context.review_input
    skill_doc = load_skill_doc("collect_context")
    payload = {
        "task_id": review_input.task_id,
        "title": review_input.title,
        "project_root": review_input.project_root,
        "source_branch": review_input.source_branch,
        "target_branch": review_input.target_branch,
        "description": review_input.description,
        "commit_messages": review_input.commit_messages,
        "requirements_doc": review_input.requirements_doc,
        "previous_report": review_input.previous_report,
        "changed_files": changed_file_payload(review_input.diff_content),
        "diff_content": review_input.diff_content,
    }
    return (
        f"{skill_doc}\n\n"
        "以下是本次运行输入：\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        "请严格按照上方 SKILL.md 执行，并返回指定的 JSON 格式输出。"
    )


def _parse_context_text(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        return {"summary": "", "warnings": ["context subagent 返回空输出"]}
    for candidate in _json_candidates(stripped):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return {"summary": stripped, "warnings": ["context subagent 返回非 JSON 摘要"]}


def _json_candidates(text: str) -> list[str]:
    """按优先级给出可能是 JSON 的子串：整体 → 去围栏 → 围栏内 → 首{到末}。"""
    candidates: list[str] = [text]
    fenced = _strip_code_fence(text)
    if fenced != text:
        candidates.append(fenced)
    block = _extract_fenced_block(text)
    if block:
        candidates.append(block)
    brace = _extract_brace_span(text)
    if brace:
        candidates.append(brace)
    seen: set[str] = set()
    unique: list[str] = []
    for candidate in candidates:
        key = candidate.strip()
        if key and key not in seen:
            seen.add(key)
            unique.append(key)
    return unique


def _extract_fenced_block(text: str) -> str:
    """提取任意位置的 ```json ... ``` 或 ``` ... ``` 代码块内容。"""
    fence = text.find("```")
    if fence == -1:
        return ""
    after = text.find("\n", fence)
    if after == -1:
        return ""
    close = text.find("```", after + 1)
    if close == -1:
        return ""
    return text[after + 1 : close].strip()


def _extract_brace_span(text: str) -> str:
    """取首个 `{` 到末个 `}` 的子串作为兜底 JSON 候选。"""
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return ""
    return text[start : end + 1].strip()


def _strip_code_fence(text: str) -> str:
    if not text.startswith("```"):
        return text
    lines = text.splitlines()
    if len(lines) >= 3 and lines[-1].strip() == "```":
        return "\n".join(lines[1:-1]).strip()
    return text


def _evidence_entry(tool_name: str, args: dict[str, Any], result: ToolResult) -> dict[str, Any]:
    return {
        "source": "context_subagent_tool_call",
        "tool_name": tool_name,
        "input": args,
        "ok": bool(result.get("ok")),
        "warnings": result.get("warnings") or [],
        "error": result.get("error"),
        "summary": _summarize_tool_result(result),
        "data": result.get("data"),
    }


def _evidence_summary(entry: dict[str, Any]) -> dict[str, Any]:
    data = entry.get("data") if isinstance(entry.get("data"), dict) else {}
    content = data.get("content") if isinstance(data, dict) else None
    content_text = "" if content is None else str(content)
    summary: dict[str, Any] = {
        "tool_name": entry.get("tool_name"),
        "input": entry.get("input"),
        "ok": entry.get("ok"),
        "summary": entry.get("summary"),
        "warnings": entry.get("warnings") or [],
        "error": entry.get("error"),
    }
    for key in ("path", "start_line", "end_line"):
        value = data.get(key)
        if value not in (None, "", []):
            summary[key] = value
    if content_text:
        summary["content_bytes"] = len(content_text.encode("utf-8"))
        summary["content_sha256"] = hashlib.sha256(
            content_text.encode("utf-8")
        ).hexdigest()
        summary["excerpt"] = _limit_text(content_text, _MAX_EVIDENCE_EXCERPT)
    return summary


def _summarize_tool_result(result: ToolResult) -> str:
    if not result.get("ok"):
        return str(result.get("error") or "tool failed")
    data = result.get("data")
    if isinstance(data, dict):
        for key in ("matches", "results", "lines"):
            value = data.get(key)
            if isinstance(value, list):
                return f"{len(value)} {key}"
        if "content" in data:
            return f"content bytes={len(str(data.get('content') or ''))}"
    return "tool succeeded"


def _section(report: dict[str, Any], key: str, tool_match: str, evidence: list[dict[str, Any]]) -> list[Any]:
    """显式提供则直接采用；否则按工具名回退收集 evidence。

    `tool_match` 既支持精确名（如 `semble_search`），也支持前缀（如 `crg_`），
    后者用于把 `crg_query/callers/callees/affected_flows/get_flow` 全部归入调用图上下文。
    """
    explicit = report.get(key)
    if isinstance(explicit, list):
        return explicit
    return [
        entry
        for entry in evidence
        if str(entry.get("tool_name") or "").startswith(tool_match)
    ]


def _code_snippets(report: dict[str, Any], evidence: list[dict[str, Any]]) -> list[Any]:
    explicit = report.get("code_snippets")
    if isinstance(explicit, list):
        return explicit
    return [
        entry
        for entry in evidence
        if entry.get("tool_name") in {"read_file", "read_file_range"}
    ]


def _compact_changed_files(changed_files: Any) -> list[dict[str, Any]]:
    if not isinstance(changed_files, list):
        return []
    compact: list[dict[str, Any]] = []
    for item in changed_files:
        if not isinstance(item, dict):
            continue
        new_path = str(item.get("new_path") or item.get("path") or "")
        old_path = str(item.get("old_path") or "")
        entry: dict[str, Any] = {}
        if old_path:
            entry["old_path"] = old_path
        if new_path:
            entry["new_path"] = new_path
            entry["path"] = new_path
        status = item.get("status")
        if status:
            entry["status"] = status
        entry["added_ranges"] = _line_ranges(item.get("added_lines"))
        compact.append(entry)
    return compact


def _line_ranges(lines: Any) -> list[list[int]]:
    if not isinstance(lines, list):
        return []
    numbers: list[int] = []
    for line in lines:
        try:
            numbers.append(int(line))
        except (TypeError, ValueError):
            continue
    if not numbers:
        return []

    ranges: list[list[int]] = []
    start = previous = sorted(set(numbers))[0]
    for line in sorted(set(numbers))[1:]:
        if line == previous + 1:
            previous = line
            continue
        ranges.append([start, previous])
        start = previous = line
    ranges.append([start, previous])
    return ranges


def _compact_context_entries(entries: Any) -> list[dict[str, Any]]:
    if not isinstance(entries, list):
        return []
    compact: list[dict[str, Any]] = []
    for item in entries[:_MAX_CONTEXT_ENTRIES]:
        if isinstance(item, dict):
            compact.append(_compact_mapping(item, text_limit=_MAX_CONTEXT_TEXT))
        else:
            compact.append({"summary": _limit_text(item, _MAX_CONTEXT_TEXT)})
    return compact


def _compact_code_snippets(snippets: Any) -> list[dict[str, Any]]:
    if not isinstance(snippets, list):
        return []
    compact: list[dict[str, Any]] = []
    for item in snippets[:_MAX_SNIPPETS]:
        if not isinstance(item, dict):
            compact.append({"excerpt": _limit_text(item, _MAX_SNIPPET_TEXT)})
            continue
        entry = _snippet_entry(item)
        if entry:
            compact.append(entry)
    return compact


def _snippet_entry(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    entry: dict[str, Any] = {}
    for key in ("path", "start_line", "end_line", "source", "reason", "summary", "why_relevant"):
        value = item.get(key, data.get(key))
        if value not in (None, "", []):
            entry[key] = value

    content = item.get("content", item.get("text", data.get("content", data.get("text"))))
    if content not in (None, ""):
        excerpt, truncated = _limited_text_with_flag(content, _MAX_SNIPPET_TEXT)
        entry["excerpt"] = excerpt
        if truncated:
            entry["truncated"] = True
    return entry


def _compact_mapping(item: dict[str, Any], *, text_limit: int) -> dict[str, Any]:
    keep = {
        "source",
        "tool_name",
        "query",
        "summary",
        "path",
        "file_path",
        "start_line",
        "end_line",
        "why_relevant",
        "related_changed_files",
        "flow_name",
        "flow_id",
    }
    compact: dict[str, Any] = {}
    for key, value in item.items():
        if key in keep:
            compact[key] = _compact_value(value, text_limit=text_limit)
        elif key in {"content", "text", "result", "matches", "results", "lines"}:
            compact[key] = _compact_value(value, text_limit=text_limit)
    return compact


def _compact_value(value: Any, *, text_limit: int) -> Any:
    if isinstance(value, str):
        return _limit_text(value, text_limit)
    if isinstance(value, list):
        return [_compact_value(item, text_limit=text_limit) for item in value[:8]]
    if isinstance(value, dict):
        return _compact_mapping(value, text_limit=text_limit)
    return value


def _limit_text(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    return text if len(text) <= limit else text[:limit] + "\n...[truncated]"


def _limited_text_with_flag(value: Any, limit: int) -> tuple[str, bool]:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n...[truncated]", True


def _collect_warnings(evidence: list[dict[str, Any]], report: dict[str, Any]) -> list[str]:
    warnings: list[str] = []
    for item in report.get("warnings") or []:
        warnings.append(str(item))
    for entry in evidence:
        for warning in entry.get("warnings") or []:
            warnings.append(str(warning))
        if entry.get("ok") is False and entry.get("error"):
            warnings.append(str(entry["error"]))
    return warnings


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _build_fallback_report(runtime_context: RuntimeContext, reason: str) -> dict[str, Any]:
    diff_content, source, diff_warnings = _fallback_diff_content(runtime_context)
    warnings = [f"context subagent 失败: {reason}", *diff_warnings]
    if not diff_content:
        missing = "diff missing: review_input.diff_content、diff_file_path、workspace changes.diff 均为空或不可读"
        warnings.append(missing)
        return {
            "summary": "context subagent 失败，且无法读取 diff；停止后续维度审查。",
            "diff_summary": "",
            "warnings": warnings,
            "fatal": True,
            "error": missing,
            "_diff_content": "",
            "_diff_source": source,
        }

    changed_files = changed_file_payload(diff_content)
    diff_summary = _local_diff_summary(diff_content)
    if not changed_files or not diff_summary:
        error = "fallback context failed: unable to generate changed_files or diff_summary"
        warnings.append(error)
        return {
            "summary": "context subagent 失败，且无法生成有效 diff 摘要；停止后续维度审查。",
            "diff_summary": diff_summary,
            "warnings": warnings,
            "fatal": True,
            "error": error,
            "_diff_content": diff_content,
            "_diff_source": source,
        }

    report: dict[str, Any] = {
        "summary": "context subagent 失败；使用本地 compact fallback context。",
        "diff_summary": diff_summary,
        "semantic_context": [],
        "call_graph_context": [],
        "code_snippets": [],
        "warnings": warnings,
        "_diff_content": diff_content,
        "_diff_source": source,
    }
    if len(diff_content.encode("utf-8")) <= _FALLBACK_FULL_DIFF_BYTES:
        report["diff_excerpt"] = diff_content
    else:
        report["changed_line_snippets"] = _changed_line_snippets(diff_content)
    return report


def _fatal_fallback_report(*, original_error: str, fallback_error: str) -> dict[str, Any]:
    warning = (
        "context subagent 失败，且 fallback context 构造失败: "
        f"original={original_error}; fallback={fallback_error}"
    )
    return {
        "summary": "context subagent 失败，且 fallback context 构造失败；停止后续维度审查。",
        "diff_summary": "",
        "warnings": [warning],
        "fatal": True,
        "error": warning,
        "_diff_content": "",
        "_diff_source": "fallback_failed",
    }


def _fallback_diff_content(runtime_context: RuntimeContext) -> tuple[str, str, list[str]]:
    review_input = runtime_context.review_input
    if review_input.diff_content:
        return review_input.diff_content, "context.json.diff_content", []

    warnings: list[str] = []
    if review_input.diff_file_path:
        diff_path = _safe_workspace_path(
            runtime_context.workspace_dir,
            review_input.diff_file_path,
        )
        if diff_path is None:
            warnings.append(
                "diff_file_path rejected: path escapes workspace_dir: "
                f"{review_input.diff_file_path}"
            )
        else:
            try:
                if diff_path.is_file():
                    return diff_path.read_text(encoding="utf-8"), str(diff_path), warnings
                warnings.append(f"diff_file_path not found: {diff_path}")
            except OSError as exc:
                warnings.append(f"diff_file_path unreadable: {diff_path}: {exc}")

    workspace_diff = runtime_context.workspace_dir / "changes.diff"
    try:
        if workspace_diff.is_file():
            return workspace_diff.read_text(encoding="utf-8"), str(workspace_diff), warnings
        warnings.append(f"workspace changes.diff not found: {workspace_diff}")
    except OSError as exc:
        warnings.append(f"workspace changes.diff unreadable: {workspace_diff}: {exc}")
    return "", "missing", warnings


def _safe_workspace_path(workspace_dir: Path, raw_path: str) -> Path | None:
    try:
        root = workspace_dir.resolve()
        raw = Path(raw_path)
        candidate = raw.resolve() if raw.is_absolute() else (root / raw).resolve()
    except OSError:
        return None
    if candidate == root or candidate.is_relative_to(root):
        return candidate
    return None


def _changed_line_snippets(diff_content: str) -> list[dict[str, Any]]:
    snippets: list[dict[str, Any]] = []
    current_file = ""
    current_start = 0
    current_lines: list[str] = []

    def flush() -> None:
        nonlocal current_start, current_lines
        if current_file and current_lines:
            snippets.append(
                {
                    "path": current_file,
                    "start_line": current_start,
                    "excerpt": "\n".join(current_lines[:_FALLBACK_SNIPPET_LINES]),
                }
            )
        current_start = 0
        current_lines = []

    for line in diff_content.splitlines():
        if line.startswith("diff --git "):
            flush()
            parts = line.split()
            current_file = parts[3][2:] if len(parts) >= 4 and parts[3].startswith("b/") else ""
            continue
        if line.startswith("@@"):
            flush()
            current_start = _hunk_new_start(line)
            continue
        if line.startswith("+") and not line.startswith("+++"):
            if len(current_lines) < _FALLBACK_SNIPPET_LINES:
                current_lines.append(line)
    flush()
    return snippets[:_MAX_SNIPPETS]


def _hunk_new_start(line: str) -> int:
    marker = "+"
    idx = line.find(marker)
    if idx == -1:
        return 0
    number = []
    for char in line[idx + 1:]:
        if char.isdigit():
            number.append(char)
            continue
        break
    return int("".join(number)) if number else 0


def _local_diff_summary(diff_content: str) -> str:
    changed = changed_file_payload(diff_content)
    files = [str(item.get("path") or item.get("new_path") or item) for item in changed]
    if not files:
        return "No changed files parsed from diff."
    preview = ", ".join(files[:20])
    suffix = "" if len(files) <= 20 else f", ... ({len(files)} files total)"
    return f"Changed files: {preview}{suffix}"
