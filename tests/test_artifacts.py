from __future__ import annotations

from cr_agent.core.artifacts import (
    append_run_log,
    write_result_json,
    write_result_markdown,
)
from cr_agent.core.review_output import LineComments, ReviewResult, TokenUsage, load_review_result


def test_artifacts_write_result_markdown_and_log(tmp_path) -> None:
    result = ReviewResult(
        status="success",
        llm_result="# report",
        log_path=str(tmp_path / "run.log"),
        tokens_consume=TokenUsage(input_tokens=1, output_tokens=2, cost=0.0),
        line_comments=LineComments(comments=[]),
        issues=[],
    )

    result_path = write_result_json(tmp_path, result)
    markdown_path = write_result_markdown(tmp_path, "# report")
    log_path = append_run_log(tmp_path, "review started")

    assert load_review_result(result_path).status == "success"
    assert markdown_path.read_text(encoding="utf-8") == "# report"
    assert "review started" in log_path.read_text(encoding="utf-8")

