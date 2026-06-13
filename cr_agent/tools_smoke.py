"""
PR4 手动 real-SDK 冒烟:把 cr-native 工具挂到真实主 agent 上端到端验收。

与单测(脚本化、不联网)不同,本入口经 Claude Agent SDK(走 LiteLLM 网关)拉起真实主 agent,
让它调用 facade.tools_for("context") 暴露的 cr-native 工具(read_file / glob_files / grep_text 等)。

前置:同 `cr_agent.smoke`(claude CLI + 运行中的 LiteLLM 网关 + 填好 [llm];project_root 指向真实仓库)。

运行:
  python -m cr_agent.tools_smoke --config <abs path to agent_config.toml> --platform gitlab

观察:
  - 终端打印 agent 最终文本与四项 usage;
  - run.log 可见 TOOL_CALL_START/END(read_file / glob_files / grep_text)与结构化结果、无 api_key 明文。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from cr_agent.bootstrap import bootstrap_runtime
from cr_agent.core.agent_config import VALID_PLATFORMS
from cr_agent.core.errors import RuntimeTimeoutError
from cr_agent.core.main_agent import build_main_agent_runtime
from cr_agent.tools.provider import ToolSpec

_IMPLEMENTED_TOOL_NAMES = {"read_file", "read_file_range", "glob_files", "grep_text"}
_DEFAULT_TIMEOUT_S = 120.0
_DEFAULT_MAX_TURNS = 6

_USER_PROMPT = (
    "Use the available tools exactly once each in this order: "
    "1) call glob_files with pattern '**/*'; "
    "2) call read_file on one returned file; "
    "3) call grep_text with pattern 'TODO'. "
    "Then summarize what you found in one sentence and stop."
)
_SYSTEM_PROMPT = "You are a code exploration agent. Use only the provided tools."


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="CR-Agent PR4 cr-native tools smoke")
    parser.add_argument("--config", required=True, help="Path to workspace agent_config.toml")
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=_DEFAULT_TIMEOUT_S,
        help=f"Overall SDK call timeout in seconds. Default: {_DEFAULT_TIMEOUT_S:g}",
    )
    parser.add_argument(
        "--max-turns",
        type=int,
        default=_DEFAULT_MAX_TURNS,
        help=f"Max Claude turns before stopping. Default: {_DEFAULT_MAX_TURNS}",
    )
    parser.add_argument(
        "--platform",
        choices=sorted(VALID_PLATFORMS),
        default=None,
        help="Optional platform override.",
    )
    return parser


def _implemented_tools(tools: list[Any]) -> list[Any]:
    return [tool for tool in tools if tool.name in _IMPLEMENTED_TOOL_NAMES]


def _trace_tools(tools: list[ToolSpec]) -> list[ToolSpec]:
    traced: list[ToolSpec] = []
    for tool in tools:
        original_handler = tool.handler

        async def traced_handler(args: dict[str, Any], *, _tool=tool, _handler=original_handler):
            print(f"[tools_smoke] tool_start name={_tool.name} args={args}", flush=True)
            result = await _handler(args)
            print(
                f"[tools_smoke] tool_end name={_tool.name} ok={result.get('ok')} "
                f"warnings={len(result.get('warnings') or [])}",
                flush=True,
            )
            return result

        traced.append(
            ToolSpec(
                name=tool.name,
                description=tool.description,
                input_schema=tool.input_schema,
                handler=traced_handler,
            )
        )
    return traced


def main() -> int:
    args = _build_parser().parse_args()

    runtime_context = bootstrap_runtime(
        config_path=Path(args.config).expanduser().resolve(),
        platform_override=args.platform,
    )

    # cr-native 工具从 facade 取(allowlist 过滤后),不经 skill。
    project_root = Path(runtime_context.review_input.project_root)
    if not project_root.exists():
        print(
            "[tools_smoke] project_root does not exist: "
            f"{project_root}\n"
            "Update context.json project_root to a local absolute path before running "
            "the real-SDK tools smoke.",
            file=sys.stderr,
        )
        return 2

    tools = _trace_tools(_implemented_tools(runtime_context.tool_facade.tools_for("context")))
    if not tools:
        print("[tools_smoke] no implemented cr-native tools exposed", file=sys.stderr)
        return 2

    agent_runtime = build_main_agent_runtime(runtime_context.config)

    try:
        result = asyncio.run(
            agent_runtime.run_review_loop(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=_USER_PROMPT,
                skill_tools=tools,
                can_use_tool=None,
                timeout_s=args.timeout_s,
                max_turns=args.max_turns,
            )
        )
    except RuntimeTimeoutError as exc:
        print(f"[tools_smoke] timeout: {exc}", file=sys.stderr)
        print(
            "[tools_smoke] diagnosis: if no tool_start lines appeared, the model/gateway "
            "did not reach tool execution before the timeout.",
            file=sys.stderr,
        )
        return 1

    usage = result.usage
    print(f"[tools_smoke] tools_exposed={[t.name for t in tools]}")
    print(
        "[tools_smoke] input={i} output={o} cache_creation={cc} cache_read={cr}".format(
            i=usage.input_tokens,
            o=usage.output_tokens,
            cc=usage.cache_creation_tokens,
            cr=usage.cache_read_tokens,
        )
    )
    print(f"[tools_smoke] text={result.text[:500]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
