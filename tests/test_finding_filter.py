from __future__ import annotations

from cr_agent.core.finding_filter import (
    filter_dimension_result,
    filter_findings,
    flatten_filtered_findings,
    minimum_finding_score,
)


def _finding(score: int) -> dict:
    return {"title": f"issue-{score}", "score": score}


def test_minimum_finding_score_matches_summary_rule_groups() -> None:
    assert minimum_finding_score("business") == 60
    assert minimum_finding_score("security") == 60
    assert minimum_finding_score("performance") == 70
    assert minimum_finding_score("dependency") == 70
    assert minimum_finding_score("maintainability") == 70
    assert minimum_finding_score("testing") == 70
    assert minimum_finding_score("error_handling") == 70
    assert minimum_finding_score("consistency") == 80
    assert minimum_finding_score("readability") == 80
    assert minimum_finding_score("documentation") == 80


def test_filter_findings_drops_scores_below_threshold() -> None:
    findings = [_finding(59), _finding(60), _finding(61)]
    assert [item["score"] for item in filter_findings("security", findings)] == [60, 61]

    findings = [_finding(69), _finding(70), _finding(71)]
    assert [item["score"] for item in filter_findings("performance", findings)] == [70, 71]

    findings = [_finding(79), _finding(80), _finding(81)]
    assert [item["score"] for item in filter_findings("readability", findings)] == [80, 81]


def test_filter_dimension_result_updates_normalized_findings() -> None:
    result = {
        "dimension": "security",
        "status": "success",
        "score": 90,
        "findings": [_finding(55), _finding(65)],
        "normalized_findings": [_finding(55), _finding(65)],
    }

    filtered = filter_dimension_result(result)

    assert filtered["findings"] == [_finding(65)]
    assert filtered["normalized_findings"] == [_finding(65)]
    assert filtered["score"] == 90


def test_filter_dimension_result_skips_failed_dimensions() -> None:
    result = {
        "dimension": "security",
        "status": "failed",
        "findings": [_finding(95)],
    }

    assert filter_dimension_result(result) == result


def test_flatten_filtered_findings_returns_single_layer_list() -> None:
    results = [
        {
            "dimension": "security",
            "status": "success",
            "findings": [_finding(80)],
        },
        {
            "dimension": "business",
            "status": "success",
            "findings": [_finding(70), _finding(90)],
        },
        {
            "dimension": "performance",
            "status": "failed",
            "findings": [_finding(95)],
        },
    ]

    flattened = flatten_filtered_findings(results)

    assert len(flattened) == 3
    assert flattened[0]["score"] == 80
    assert "findings" not in flattened[0]
