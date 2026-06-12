from __future__ import annotations

import argparse
import sys
from pathlib import Path

from cr_agent.bootstrap import bootstrap_runtime
from cr_agent.core.agent_config import VALID_PLATFORMS
from cr_agent.core.review_output import ReviewResult, write_review_result


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

    runtime.result_dir.mkdir(parents=True, exist_ok=True)
    result_json = runtime.result_dir / "result.json"
    result_md = runtime.result_dir / "cr_result.md"
    log_path = runtime.result_dir / "run.log"

    result_payload = ReviewResult(
        status="bootstrap_ready",
        llm_result=(
            "# CR-Agent\n\n"
            "当前处于 M0 脚手架阶段，已完成启动链路、平台识别和外部契约校验。\n"
        ),
        log_path=str(log_path),
        tokens_consume={"input_tokens": 0, "output_tokens": 0, "cost": 0.0},
        line_comments={"comments": []},
        issues=[],
    )
    write_review_result(result_json, result_payload)
    result_md.write_text(
        "# CR-Agent\n\n"
        "当前处于 M0 脚手架阶段，已完成启动链路、平台识别和外部契约校验。\n\n"
        f"- platform: `{runtime.platform}`\n"
        f"- task_id: `{runtime.review_input.task_id}`\n",
        encoding="utf-8",
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())
