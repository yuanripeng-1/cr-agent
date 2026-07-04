from __future__ import annotations

from datetime import datetime
from pathlib import Path

from cr_agent.issue_resolution.contracts import (
    IssueResolutionResult,
    write_issue_resolution_result,
)


def write_resolution_result_json(result_dir: Path, result: IssueResolutionResult) -> Path:
    result_path = result_dir / "issue_resolution_result.json"
    write_issue_resolution_result(result_path, result)
    return result_path


def append_run_log(result_dir: Path, message: str) -> Path:
    result_dir.mkdir(parents=True, exist_ok=True)
    log_path = result_dir / "run.log"
    timestamp = datetime.now().isoformat(timespec="seconds")
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"[{timestamp}] {message}\n")
    return log_path
