"""审查运行统计：汇总 diff、子 Agent 与行评论校验指标并导出 JSON。"""

from __future__ import annotations

import json
import time
from typing import Any, Dict, List, Optional


class ReviewStatsCollector:
    """收集单次审查运行的结构化指标，供平台侧做用量分析与质量监控。"""

    def __init__(self, task_id: str = "", mr_iid: str = "", head_sha: str = ""):
        self._started_at = time.time()
        self._task_id = task_id
        self._mr_iid = mr_iid
        self._head_sha = head_sha
        self._diff_stats: Dict[str, Any] = {}
        self._agent_stats: Dict[str, Any] = {}
        self._validation_stats: Dict[str, Any] = {}
        self._file_metrics: List[Dict[str, Any]] = []
        self._phase_timings: Dict[str, float] = {}

    def record_diff_stats(
        self,
        *,
        file_count: int,
        original_diff_chars: int,
        filtered_diff_chars: int,
        added_lines: int = 0,
        removed_lines: int = 0,
        code_files: Optional[List[str]] = None,
    ) -> None:
        self._diff_stats = {
            "file_count": file_count,
            "original_diff_chars": original_diff_chars,
            "filtered_diff_chars": filtered_diff_chars,
            "added_lines": added_lines,
            "removed_lines": removed_lines,
            "code_files": code_files or [],
        }

    def record_agent_stats(self, report_usages: Dict[str, Any], report_map: Dict[str, str]) -> None:
        dimensions: Dict[str, Any] = {}
        failed = 0
        for dim, usage in (report_usages or {}).items():
            content = (report_map or {}).get(dim, "")
            is_error = isinstance(content, str) and content.startswith("Error calling LLM:")
            if is_error:
                failed += 1
            dimensions[dim] = {
                "prompt_tokens": (usage or {}).get("prompt_tokens", 0),
                "completion_tokens": (usage or {}).get("completion_tokens", 0),
                "total_tokens": (usage or {}).get("total_tokens", 0),
                "cost": (usage or {}).get("cost", 0.0),
                "report_chars": len(content) if isinstance(content, str) else 0,
                "status": "error" if is_error else "ok",
            }
        self._agent_stats = {
            "dimension_count": len(dimensions),
            "failed_count": failed,
            "dimensions": dimensions,
        }

    def record_validation_stats(
        self,
        *,
        total: int,
        valid: int,
        corrected: int,
        rejected: int,
        needs_review: int = 0,
    ) -> None:
        self._validation_stats = {
            "total": total,
            "valid": valid,
            "corrected": corrected,
            "rejected": rejected,
            "needs_review": needs_review,
        }

    def record_file_metric(self, path: str, line_count: int, readable: bool) -> None:
        self._file_metrics.append(
            {"path": path, "line_count": line_count, "readable": readable}
        )

    def mark_phase(self, name: str, elapsed_seconds: float) -> None:
        self._phase_timings[name] = round(elapsed_seconds, 3)

    def build_report(self, usage: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        elapsed = round(time.time() - self._started_at, 3)
        total_usage = usage or {}
        return {
            "task_id": self._task_id,
            "mr_iid": self._mr_iid,
            "head_sha": self._head_sha,
            "elapsed_seconds": elapsed,
            "phase_timings": self._phase_timings,
            "diff": self._diff_stats,
            "agents": self._agent_stats,
            "line_comment_validation": self._validation_stats,
            "file_metrics": self._file_metrics,
            "tokens": {
                "input_tokens": total_usage.get("prompt_tokens", 0),
                "output_tokens": total_usage.get("completion_tokens", 0),
                "total_tokens": total_usage.get("total_tokens", 0),
                "cost": total_usage.get("cost", 0.0),
            },
        }

    def write_json(self, path: str, usage: Optional[Dict[str, Any]] = None) -> None:
        report = self.build_report(usage)
        with open(path, "a", encoding="utf-8") as f:
            json.dump(report, f, indent=2, ensure_ascii=False)
