from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from cr_agent.bootstrap import bootstrap_runtime


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CR-Agent SDK entrypoint")
    parser.add_argument(
        "--config",
        required=True,
        help="Path to workspace agent config.toml",
    )
    parser.add_argument(
        "--platform",
        choices=["gitlab", "infcode"],
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

    # M0 阶段先输出统一占位结果，后续里程碑替换为真实审查编排流程。
    result_payload = {
        "status": "bootstrap_ready",
        "platform": runtime.platform,
        "task_id": runtime.review_input.task_id,
        "message": "M0 scaffolding ready. Orchestrator not implemented yet.",
    }

    runtime.result_dir.mkdir(parents=True, exist_ok=True)
    result_json = runtime.result_dir / "result.json"
    result_md = runtime.result_dir / "cr_result.md"

    result_json.write_text(
        json.dumps(result_payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    result_md.write_text(
        "# CR-Agent\n\n"
        "当前处于 M0 脚手架阶段，已完成启动链路与平台识别。\n\n"
        f"- platform: `{runtime.platform}`\n"
        f"- task_id: `{runtime.review_input.task_id}`\n",
        encoding="utf-8",
    )

    return 0


if __name__ == "__main__":
    sys.exit(main())

