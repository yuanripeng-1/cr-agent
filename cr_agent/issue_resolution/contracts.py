from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


IssueState = Literal["open", "solved"]
ResolutionStatus = Literal["success", "failed"]


class IssueItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_review_id: int
    discussion_id: str = ""
    note_id: int = 0
    file_path: str = Field(min_length=1)
    line_number: int = Field(ge=1)
    severity: str = ""
    comment_body: str = Field(min_length=1)
    head_sha: str = ""
    comment_url: str = ""


class IssueResolutionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    project_id: int = 0
    mr_iid: int = 0
    mr_url: str = ""
    source_branch: str = ""
    target_branch: str = ""
    merged_commit_sha: str = ""
    issues: list[IssueItem] = Field(default_factory=list)


class IssueResolutionItemResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_review_id: int
    state: IssueState
    reason: str = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)


class IssueResolutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    status: ResolutionStatus
    results: list[IssueResolutionItemResult] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


def load_issue_resolution_input(path: Path) -> IssueResolutionInput:
    data = json.loads(path.read_text(encoding="utf-8"))
    return IssueResolutionInput.model_validate(data)


def write_issue_resolution_result(path: Path, result: IssueResolutionResult) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(result.model_dump(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_issue_resolution_result(path: Path) -> IssueResolutionResult:
    data = json.loads(path.read_text(encoding="utf-8"))
    return IssueResolutionResult.model_validate(data)


def normalize_agent_results(
    raw_results: list[dict],
    *,
    known_line_review_ids: set[int],
) -> list[IssueResolutionItemResult]:
    """Filter and validate agent output; drop unknown IDs and invalid states."""
    normalized: list[IssueResolutionItemResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        line_review_id = item.get("line_review_id")
        state = item.get("state")
        reason = item.get("reason")
        if (
            not isinstance(line_review_id, int)
            or line_review_id not in known_line_review_ids
            or state not in ("open", "solved")
            or not isinstance(reason, str)
            or not reason.strip()
        ):
            continue
        confidence_raw = item.get("confidence", 0.0)
        try:
            confidence = float(confidence_raw)
        except (TypeError, ValueError):
            continue
        if confidence < 0.0 or confidence > 1.0:
            continue
        normalized.append(
            IssueResolutionItemResult(
                line_review_id=line_review_id,
                state=state,
                reason=reason.strip(),
                confidence=confidence,
            )
        )
    return normalized
