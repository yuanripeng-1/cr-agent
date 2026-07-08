from __future__ import annotations

from typing import Any

# 与 prompt/rules/summaryRule.md 中「丢弃」阈值一致：finding.score 低于该值则过滤。
_DIMENSION_MIN_SCORE: dict[str, int] = {
    "business": 60,
    "security": 60,
    "performance": 70,
    "dependency": 70,
    "maintainability": 70,
    "testing": 70,
    "error_handling": 70,
    "consistency": 80,
    "readability": 80,
    "documentation": 80,
}

_DEFAULT_MIN_SCORE = 70


def minimum_finding_score(dimension: str) -> int:
    return _DIMENSION_MIN_SCORE.get(dimension, _DEFAULT_MIN_SCORE)


def filter_findings(dimension: str, findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    threshold = minimum_finding_score(dimension)
    kept: list[dict[str, Any]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        try:
            score = int(finding.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        score = max(0, min(100, score))
        if score >= threshold:
            kept.append(finding)
    return kept


def filter_dimension_result(result: dict[str, Any]) -> dict[str, Any]:
    if result.get("status") != "success":
        return result
    dimension = str(result.get("dimension") or "")
    findings = result.get("findings")
    if not isinstance(findings, list):
        findings = []
    filtered = filter_findings(dimension, findings)
    filtered_result = dict(result)
    filtered_result["findings"] = filtered
    filtered_result["normalized_findings"] = filtered
    return filtered_result


def flatten_filtered_findings(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """将各维度过滤后的 findings 合并为单层列表，供 summary agent 使用。"""
    flattened: list[dict[str, Any]] = []
    for result in results:
        if result.get("status") != "success":
            continue
        findings = result.get("findings")
        if not isinstance(findings, list):
            continue
        for finding in findings:
            if isinstance(finding, dict):
                flattened.append(finding)
    return flattened
