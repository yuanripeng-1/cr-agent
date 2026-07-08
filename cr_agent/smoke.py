"""
PR2 手动冒烟入口:经 LiteLLM Anthropic-compatible Gateway 跑通一次真实模型调用。

与默认 `python -m cr_agent.main` 不同:main 走占位流程、不发起模型调用;本入口
构造真实 Runtime 并通过 run_review 发起恰好一次 query_main(skills 仍为占位、非模型调用),
随后写出 result.json / cr_result.md / run.log。

前置:
  1. 已安装 claude CLI(见 INSTALL.sh)。
  2. 有一个运行中的 LiteLLM Anthropic-compatible Gateway。
  3. 目标 workspace/<task>/agent_config.toml 的 [llm] 已填:
       model    = "<gateway 可路由的模型名>"
       api_key  = "<gateway 鉴权 key>"   # 注入为 ANTHROPIC_API_KEY
       api_base = "<gateway 地址>"        # 注入为 ANTHROPIC_BASE_URL

运行:
  python -m cr_agent.smoke --config <abs path to agent_config.toml> --platform gitlab
    测试本程序的时候，可以使用workspace/ 下的一个任务目录：
    比如 workspace/15-3de6a54a/
    修改：agent_config.toml中的json_path和result_path为本地路径
    例如：
    ```toml
    json_path = "/Users/liyu/Desktop/MyCodeEnv/code/crAgent/cr-agent/workspace/15-3de6a54a/context.json"
    result_path = "/Users/liyu/Desktop/MyCodeEnv/code/crAgent/cr-agent/workspace/cr_result/15-3de6a54a"
    ```
    然后运行：
    ```bash
    python -m cr_agent.smoke --config /Users/liyu/Desktop/MyCodeEnv/code/crAgent/cr-agent/workspace/15-3de6a54a/agent_config.toml --platform gitlab
    ```


观察:
  - 终端打印 status 与四项 usage(input/output/cache_creation/cache_read);
  - result.json / run.log 生成;失败时同样有产物且 token 为真实累计值;
  - run.log 中只见 api_key_configured=true,无明文 key。

"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from cr_agent.bootstrap import bootstrap_runtime
from cr_agent.core.agent_config import VALID_PLATFORMS
from cr_agent.core.main_agent import build_main_agent_runtime
from cr_agent.core.orchestrator import run_review


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CR-Agent PR2 LiteLLM smoke")
    parser.add_argument("--config", required=True, help="Path to workspace agent_config.toml")
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=300,
        help="Overall model call timeout in seconds. Default: 300",
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

    runtime_context = bootstrap_runtime(
        config_path=Path(args.config).expanduser().resolve(),
        platform_override=args.platform,
    )

    agent_runtime = build_main_agent_runtime(runtime_context.config)
    result = asyncio.run(
        run_review(
            runtime_context,
            agent_runtime=agent_runtime,
            main_timeout_s=args.timeout_s,
        )
    )

    usage = result.tokens_consume
    print(
        "[smoke] status={status} input={i} output={o} "
        "cache_creation={cc} cache_read={cr}".format(
            status=result.status,
            i=usage.input_tokens,
            o=usage.output_tokens,
            cc=usage.cache_creation_tokens,
            cr=usage.cache_read_tokens,
        )
    )
    print(f"[smoke] result_dir={runtime_context.result_dir}")
    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
