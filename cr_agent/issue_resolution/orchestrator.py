from __future__ import annotations

from cr_agent.issue_resolution.agent import run_resolution_agent
from cr_agent.issue_resolution.artifacts import append_run_log, write_resolution_result_json
from cr_agent.issue_resolution.bootstrap import IssueResolutionRuntimeContext
from cr_agent.issue_resolution.contracts import IssueResolutionResult
from cr_agent.utils.logging import get_logger

_logger = get_logger("cr_agent.issue_resolution.orchestrator")


async def run_resolution_check(
    runtime_context: IssueResolutionRuntimeContext,
) -> IssueResolutionResult:
    task_id = runtime_context.resolution_input.task_id
    append_run_log(runtime_context.result_dir, "issue_resolution started")

    try:
        results = await run_resolution_agent(runtime_context)
        outcome = IssueResolutionResult(
            task_id=task_id,
            status="success",
            results=results,
            errors=[],
        )
        write_resolution_result_json(runtime_context.result_dir, outcome)
        append_run_log(
            runtime_context.result_dir,
            f"issue_resolution finished status=success results={len(results)}",
        )
        _logger.info(
            "ISSUE_RESOLUTION_DONE task_id=%s status=success results=%s",
            task_id,
            len(results),
        )
        return outcome
    except Exception as exc:
        _logger.exception("ISSUE_RESOLUTION_FAILED task_id=%s error=%s", task_id, exc)
        outcome = IssueResolutionResult(
            task_id=task_id,
            status="failed",
            results=[],
            errors=[str(exc)],
        )
        try:
            write_resolution_result_json(runtime_context.result_dir, outcome)
        except Exception:
            _logger.exception("ISSUE_RESOLUTION_WRITE_FAILED task_id=%s", task_id)
        append_run_log(
            runtime_context.result_dir,
            f"issue_resolution finished status=failed error={exc}",
        )
        return outcome
