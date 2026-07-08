from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from cr_agent.core.agent_config import VALID_PLATFORMS
from cr_agent.issue_resolution.artifacts import append_run_log, write_resolution_result_json
from cr_agent.issue_resolution.bootstrap import bootstrap_issue_resolution
from cr_agent.issue_resolution.contracts import IssueResolutionResult
from cr_agent.issue_resolution.orchestrator import run_resolution_check
from cr_agent.utils.logging import get_logger

_logger = get_logger("cr_agent.issue_resolution.main")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CR-Agent issue resolution entrypoint")
    parser.add_argument(
        "--config",
        required=True,
        help="Path to issue_resolution agent_config.toml",
    )
    parser.add_argument(
        "--platform",
        choices=sorted(VALID_PLATFORMS),
        default=None,
        help="Optional platform override. Higher priority than config.",
    )
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=1200,
        help="Resolution agent timeout in seconds (reserved for future use).",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    _ = args.timeout_s  # reserved hook; subagent timeout comes from config.timeouts

    runtime = bootstrap_issue_resolution(
        config_path=Path(args.config).expanduser().resolve(),
        platform_override=args.platform,
    )

    print(
        f"[issue_resolution/bootstrap] platform={runtime.platform} "
        f"context={runtime.context_path} result_dir={runtime.result_dir} "
        f"issues={len(runtime.resolution_input.issues)}"
    )

    try:
        result = asyncio.run(run_resolution_check(runtime))
    except Exception as exc:
        _logger.exception("ISSUE_RESOLUTION_FATAL_ERROR: %s", exc)
        result = IssueResolutionResult(
            task_id=runtime.resolution_input.task_id,
            status="failed",
            results=[],
            errors=[str(exc)],
        )
        try:
            write_resolution_result_json(runtime.result_dir, result)
        except Exception:
            _logger.exception("ISSUE_RESOLUTION_FATAL_WRITE_FAILED")
        try:
            append_run_log(runtime.result_dir, f"issue_resolution fatal error: {exc}")
        except Exception:
            _logger.exception("ISSUE_RESOLUTION_FATAL_APPEND_LOG_FAILED")
        return 1
    finally:
        gateway = getattr(runtime, "litellm_gateway", None)
        if gateway is not None:
            gateway.stop()

    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
