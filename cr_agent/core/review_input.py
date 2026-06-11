from __future__ import annotations

from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ReviewInput:
    task_id: str
    project_id: int | None
    mr_iid: int | None
    title: str
    description: str
    base_sha: str
    head_sha: str
    start_sha: str
    source_branch: str
    target_branch: str
    diff_content: str
    diff_file_path: str
    project_root: str
    previous_report: str
    requirements_doc: str
    platform: str | None
    commit_messages: list[str] = field(default_factory=list)


def _normalize_commit_messages(raw_value: Any) -> list[str]:
    if raw_value is None:
        return []
    if isinstance(raw_value, list):
        return [str(item).strip() for item in raw_value if str(item).strip()]
    if isinstance(raw_value, str):
        stripped = raw_value.strip()
        return [stripped] if stripped else []
    return []


def load_review_input(context_path: Path) -> ReviewInput:
    payload = json.loads(context_path.read_text(encoding="utf-8"))

    return ReviewInput(
        task_id=str(payload.get("task_id", "")),
        project_id=payload.get("project_id"),
        mr_iid=payload.get("mr_iid"),
        title=str(payload.get("title", "")),
        description=str(payload.get("description", "")),
        base_sha=str(payload.get("base_sha", "")),
        head_sha=str(payload.get("head_sha", "")),
        start_sha=str(payload.get("start_sha", "")),
        source_branch=str(payload.get("source_branch", "")),
        target_branch=str(payload.get("target_branch", "")),
        diff_content=str(payload.get("diff_content", "")),
        diff_file_path=str(payload.get("diff_file_path", "")),
        project_root=str(payload.get("project_root", "")),
        previous_report=str(payload.get("previous_report", "")),
        requirements_doc=str(payload.get("requirements_Doc", "")),
        platform=payload.get("platform"),
        commit_messages=_normalize_commit_messages(payload.get("commit_messages")),
    )

