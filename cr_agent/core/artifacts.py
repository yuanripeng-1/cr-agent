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


def _run_log_messages(log_path: Path) -> list[str]:
    if not log_path.exists():
        return []
    messages: list[str] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        if "] " in line:
            messages.append(line.split("] ", 1)[1])
        elif line.strip():
            messages.append(line.strip())
    return messages


def review_in_progress(result_dir: Path) -> bool:
    """
    根据 run.log 判断是否有尚未结束的完整审查。

    orchestrator 在审查开始/结束时分别追加 review started / review finished status=...
    """
    messages = _run_log_messages(result_dir / "run.log")
    started = sum(1 for message in messages if message == "review started")
    finished = sum(1 for message in messages if message.startswith("review finished status="))
    return started > finished

