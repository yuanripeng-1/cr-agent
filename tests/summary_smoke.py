#!/usr/bin/env python3
"""
Summary agent 单独冒烟:从已有 summary_prompt.txt 发起一次汇总调用。

与 `python -m cr_agent.main` 不同:不跑完整审查流水线,仅复用 summarize skill
的 invoke_summary_agent() 调用逻辑(含 structured output / SDK 分支)。

非 pytest 用例;文件名不以 test_ 开头,全量 pytest 不会收集本脚本。

前置:
  1. 使用项目标准 conda 环境 cragent(见 INSTALL.sh);勿用 base env。
  2. 已安装 claude CLI(见 INSTALL.sh)。
  3. [llm] 已配置 model / api_key / api_base(或 use_litellm_gateway)。
  4. 准备好 summary_prompt.txt(可由完整流程产出,或手工放置)。

运行(在项目根目录):
  conda run -n cragent python tests/summary_smoke.py \\
    --config <abs path to agent_config.toml> \\
    --prompt-path <abs path to summary_prompt.txt> \\
    --output-txt <abs path to summary_report.txt> \\
    [--output-json <abs path to summary_report.json>] \\
    [--platform gitlab]

示例:
  conda run -n cragent python tests/summary_smoke.py \\
    --config workspaces/49-3d646d13/agent_config.toml \\
    --prompt-path workspaces/49-3d646d13/cr_result/summary_prompt.txt \\
    --output-txt workspaces/49-3d646d13/cr_result/temp/summary_report.txt \\
    --platform gitlab
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from cr_agent.bootstrap import bootstrap_runtime
from cr_agent.core.agent_config import VALID_PLATFORMS
from cr_agent.skills.summarize.skill import invoke_summary_agent


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CR-Agent summary agent smoke")
    parser.add_argument("--config", required=True, help="Path to workspace agent_config.toml")
    parser.add_argument(
        "--prompt-path",
        required=True,
        help="Path to summary_prompt.txt (input context for summary agent)",
    )
    parser.add_argument(
        "--output-txt",
        required=True,
        help="Path to write summary_report.txt (raw model output)",
    )
    parser.add_argument(
        "--output-json",
        default="",
        help="Path to write summary_report.json; default: <output-txt>.json sibling",
    )
    parser.add_argument(
        "--platform",
        choices=sorted(VALID_PLATFORMS),
        default=None,
        help="Optional platform override.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()

    config_path = Path(args.config).expanduser().resolve()
    prompt_path = Path(args.prompt_path).expanduser().resolve()
    output_txt = Path(args.output_txt).expanduser().resolve()
    if args.output_json:
        output_json = Path(args.output_json).expanduser().resolve()
    else:
        output_json = output_txt.with_suffix(".json")

    if not prompt_path.is_file():
        print(f"[summary_smoke] prompt file not found: {prompt_path}", file=sys.stderr)
        return 2

    runtime_context = bootstrap_runtime(
        config_path=config_path,
        platform_override=args.platform,
    )

    prompt = prompt_path.read_text(encoding="utf-8")
    report = asyncio.run(
        invoke_summary_agent(
            runtime_context,
            prompt,
            validation_errors=None,
            summary_report_txt_path=output_txt,
            summary_report_json_path=output_json,
        )
    )

    usage = report.get("usage") or {}
    print(
        "[summary_smoke] llm_result_chars={chars} input={i} output={o}".format(
            chars=len(report.get("llm_result", "") or ""),
            i=usage.get("input_tokens", 0),
            o=usage.get("output_tokens", 0),
        )
    )
    print(f"[summary_smoke] summary_report_txt={output_txt}")
    print(f"[summary_smoke] summary_report_json={output_json}")
    if report.get("structured_output_error_kind"):
        print(
            "[summary_smoke] structured_output_error_kind="
            f"{report['structured_output_error_kind']}"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
