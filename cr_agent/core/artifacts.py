from __future__ import annotations

from datetime import datetime
from pathlib import Path

from cr_agent.core.review_output import ReviewResult, write_review_result


def write_result_json(result_dir: Path, result: ReviewResult) -> Path:
    result_dir.mkdir(parents=True, exist_ok=True)
    result_path = result_dir / "result.json"
    write_review_result(result_path, result)
    return result_path


def write_result_markdown(result_dir: Path, content: str) -> Path:
    result_dir.mkdir(parents=True, exist_ok=True)
    result_path = result_dir / "cr_result.md"
    result_path.write_text(content, encoding="utf-8")
    return result_path


def append_run_log(result_dir: Path, message: str) -> Path:
    result_dir.mkdir(parents=True, exist_ok=True)
    log_path = result_dir / "run.log"
    timestamp = datetime.now().isoformat(timespec="seconds")
    with log_path.open("a", encoding="utf-8") as log_file:
        log_file.write(f"[{timestamp}] {message}\n")
    return log_path

