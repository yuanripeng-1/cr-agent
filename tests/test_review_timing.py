from cr_agent.core.review_timing import ReviewTiming, format_review_timing_summary


def test_format_review_timing_summary() -> None:
    timing = ReviewTiming(
        context_s=12.5,
        context_skill_s=13.0,
        dimension_s={"security": 30.0, "testing": 45.0},
        dimension_skill_s=50.0,
        summary_attempts_s=[20.0, 15.0],
        summary_skill_s=[21.0, 16.0],
        main_s=5.0,
    )
    summary = format_review_timing_summary(timing)
    assert "context_s=12.50" in summary
    assert "dimension_total_s=75.00" in summary
    assert "summary_total_s=35.00" in summary
    assert "main_s=5.00" in summary
