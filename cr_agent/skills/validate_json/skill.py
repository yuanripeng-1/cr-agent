from __future__ import annotations

from typing import Any
from pathlib import PurePosixPath

from pydantic import ValidationError

from cr_agent.bootstrap import RuntimeContext
from cr_agent.core.review_output import ReviewResult
from cr_agent.core.types import ValidationResult
from cr_agent.utils.diff import added_line_map


async def validate_json(runtime_context: RuntimeContext, report: dict[str, Any]) -> ValidationResult:
    """
    纯代码 schema 校验。

    不调用 agent、不拿工具、不维护 attempt。attempt 仍只由主 agent 的
    summarize_report 工具 handler 计数。
    代码审查最终报告 report 是否符合 ReviewResult 的 JSON 契约——也就是后端要消费的 result.json 结构。

    """
    payload = _normalize_report(runtime_context, report)
    errors: list[str] = []
    try:
        # ReviewResult 继承自 Pydantic 的 BaseModel
        # 所以这里可以使用 model_validate 方法来验证 payload 是否符合 ReviewResult 的 JSON 契约
        ReviewResult.model_validate(payload)
    except ValidationError as exc:
        errors.extend(_format_errors(exc))
    errors.extend(_semantic_errors(runtime_context, payload))
    return ValidationResult(valid=not errors, errors=errors)


def _normalize_report(runtime_context: RuntimeContext, report: dict[str, Any]) -> dict[str, Any]:
    payload = dict(report)
    payload.setdefault("status", "success")
    payload.setdefault("log_path", str(runtime_context.result_dir / "run.log"))
    payload.setdefault("tokens_consume", {})
    payload.setdefault("line_comments", {"comments": []})
    payload.setdefault("issues", [])
    payload.setdefault("task_id", runtime_context.review_input.task_id)
    payload.setdefault("platform", runtime_context.platform)
    return payload


def _format_errors(exc: ValidationError) -> list[str]:
    errors: list[str] = []
    for item in exc.errors():
        loc = ".".join(str(part) for part in item.get("loc", ())) or "<root>"
        msg = str(item.get("msg", "invalid"))
        errors.append(f"{loc}: {msg}")
    return errors


def _semantic_errors(runtime_context: RuntimeContext, payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    comments = payload.get("line_comments", {}).get("comments", [])
    issues = payload.get("issues", [])
    locations = [
        location
        for issue in issues
        if isinstance(issue, dict)
        for location in issue.get("locations", [])
        if isinstance(location, dict)
    ]
    if len(comments) != len(locations):
        errors.append(
            "line_comments.comments: count must equal total issues.locations count"
        )

    diff_lines = added_line_map(runtime_context.review_input.diff_content)
    for index, comment in enumerate(comments):
        if not isinstance(comment, dict):
            continue
        path = str(comment.get("new_path") or "")
        errors.extend(_path_errors(f"line_comments.comments.{index}.new_path", path))
        errors.extend(
            _diff_line_errors(
                f"line_comments.comments.{index}",
                path,
                comment.get("start_line"),
                comment.get("end_line"),
                diff_lines,
            )
        )
    for issue_index, issue in enumerate(issues):
        if not isinstance(issue, dict):
            continue
        for loc_index, location in enumerate(issue.get("locations", [])):
            if not isinstance(location, dict):
                continue
            path = str(location.get("path") or "")
            prefix = f"issues.{issue_index}.locations.{loc_index}"
            errors.extend(_path_errors(f"{prefix}.path", path))
            errors.extend(
                _diff_line_errors(
                    prefix,
                    path,
                    location.get("start_line"),
                    location.get("end_line"),
                    diff_lines,
                )
            )
    return errors


def _path_errors(field: str, path: str) -> list[str]:
    if not path:
        return []
    posix = PurePosixPath(path)
    if posix.is_absolute() or ".." in posix.parts:
        return [f"{field}: path must be relative and must not contain '..'"]
    return []


def _diff_line_errors(
    prefix: str,
    path: str,
    start_line: Any,
    end_line: Any,
    diff_lines: dict[str, set[int]],
) -> list[str]:
    if not path or path not in diff_lines:
        return [f"{prefix}.path: path is not present in diff"] if path else []
    try:
        start = int(start_line)
        end = int(end_line)
    except (TypeError, ValueError):
        return []
    added = diff_lines[path]
    missing = [line for line in range(start, end + 1) if line not in added]
    if missing:
        return [f"{prefix}: line range must point to added diff lines"]
    return []
