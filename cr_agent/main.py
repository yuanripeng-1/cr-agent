from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cr_agent.bootstrap import bootstrap_runtime
from cr_agent.core.agent_config import VALID_PLATFORMS
from cr_agent.core.artifacts import (
    append_run_log,
    write_result_json,
    write_result_markdown,
)
from cr_agent.core.review_output import LineComments, ReviewResult, TokenUsage


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CR-Agent SDK entrypoint")
    parser.add_argument(
        "--config",
        required=True,
        help="Path to workspace agent config.toml",
    )
    parser.add_argument(
        "--platform",
        choices=sorted(VALID_PLATFORMS),
        default=None,
        help="Optional platform override. Higher priority than config/context.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()

    runtime = bootstrap_runtime(
        config_path=Path(args.config).expanduser().resolve(),
        platform_override=args.platform,
    )

    print(
        f"[bootstrap] platform={runtime.platform} "
        f"context={runtime.context_path} workspace={runtime.workspace_dir}"
    )

    # main() 是占位入口:只做 bootstrap 并写一份 bootstrap_ready 产物,不跑 skill、
    # 不驱动主 agent(不需 claude CLI)。真实 agentic 审查请用 `python -m cr_agent.smoke`。
    result = ReviewResult(
        status="bootstrap_ready",
        llm_result="# CR-Agent\n\nBootstrap ready. Run `python -m cr_agent.smoke` for the agentic review.",
        log_path=str(runtime.result_dir / "run.log"),
        tokens_consume=TokenUsage(),
        line_comments=LineComments(comments=[]),
        issues=[],
        task_id=runtime.review_input.task_id,
        platform=runtime.platform,
    )
    write_result_json(runtime.result_dir, result)
    write_result_markdown(runtime.result_dir, result.llm_result)
    append_run_log(runtime.result_dir, "bootstrap_ready")
    return 0


if __name__ == "__main__":
    sys.exit(main())
