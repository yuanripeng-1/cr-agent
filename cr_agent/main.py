from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from cr_agent.bootstrap import bootstrap_runtime
from cr_agent.core.agent_config import VALID_PLATFORMS
from cr_agent.core.artifacts import (
    append_run_log,
    review_in_progress,
    write_result_json,
    write_result_markdown,
)
from cr_agent.core.main_agent import build_main_agent_runtime
from cr_agent.core.orchestrator import run_review
from cr_agent.core.review_output import LineComments, ReviewResult, TokenUsage
from cr_agent.utils.logging import get_logger

_logger = get_logger("cr_agent.main")


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
    parser.add_argument(
        "--bootstrap-only",
        action="store_true",
        help="Only validate bootstrap and write bootstrap_ready artifacts.",
    )
    parser.add_argument(
        "--timeout-s",
        type=float,
        default=3000,
        help="Main agent timeout in seconds.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()

    # 创建 runtime：RuntimeContext，本次审查运行的上下文环境。
    runtime = bootstrap_runtime(
        config_path=Path(args.config).expanduser().resolve(),
        platform_override=args.platform,
    )

    print(
        f"[bootstrap] platform={runtime.platform} "
        f"context={runtime.context_path} workspace={runtime.workspace_dir}"
    )

    # --bootstrap-only:装配冒烟模式。只验证 bootstrap 是否成功(config/context/平台/
    # 工具/网关/runtime 都装配妥当),不跑审查、不调模型,写一份 status="bootstrap_ready"
    # 的占位产物后 exit 0。供 RUN.sh 调试与 CI 在不消耗模型额度的前提下做健康检查。
    if args.bootstrap_only:
        # 若已有审查在进行(产物正在被写),不要用占位产物覆盖真实 result.json / cr_result.md。
        if review_in_progress(runtime.result_dir):
            print(
                "[bootstrap] review in progress; skipping result.json / cr_result.md overwrite"
            )
            append_run_log(
                runtime.result_dir,
                "bootstrap_ready skipped: review in progress",
            )
            return 0

        result = ReviewResult(
            status="bootstrap_ready",
            llm_result="# CR-Agent\n\nBootstrap ready.",
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

    try:
        # 创建 主 agent：SdkMainAgentRuntime
        agent_runtime = build_main_agent_runtime(runtime.config)
        result = asyncio.run(
            # 调用 orchestrator：run_review，主 agent 驱动的审查编排。
            run_review(
                runtime,
                agent_runtime=agent_runtime,
                main_timeout_s=args.timeout_s,
            )
        )
    except Exception as exc:
        # 顶层兜底:run_review 内部有写盘保护,但 build_main_agent_runtime / asyncio.run
        # 等环节抛错会绕过它,导致进程崩溃且不写任何产物。这里构造 status="error" 的
        # ReviewResult 并落盘,保证下游 CI/平台总能读到结构化结果。
        # 先用标准 logger 记含 traceback 的根因:即使后续写盘失败,根因也不丢。
        _logger.exception("REVIEW_FATAL_ERROR: %s", exc)
        result = ReviewResult(
            status="error",
            llm_result="# CR-Agent\n\nReview failed before completion.",
            log_path=str(runtime.result_dir / "run.log"),
            tokens_consume=TokenUsage(),
            line_comments=LineComments(comments=[]),
            issues=[],
            task_id=runtime.review_input.task_id,
            platform=runtime.platform,
            errors=[str(exc)],
        )
        # 写盘各自包一层:任一写盘失败只记录,不再次抛出掩盖上面已记录的原始异常。
        try:
            write_result_json(runtime.result_dir, result)
        except Exception:
            _logger.exception("REVIEW_FATAL_ERROR_WRITE_JSON_FAILED")
        try:
            write_result_markdown(runtime.result_dir, result.llm_result)
        except Exception:
            _logger.exception("REVIEW_FATAL_ERROR_WRITE_MD_FAILED")
        try:
            append_run_log(runtime.result_dir, f"review error: {exc}")
        except Exception:
            _logger.exception("REVIEW_FATAL_ERROR_APPEND_LOG_FAILED")
        return 1
    finally:
        # 关闭可选的本地 LiteLLM proxy 网关(若已启动);atexit 兜底。
        gateway = getattr(runtime, "litellm_gateway", None)
        if gateway is not None:
            gateway.stop()
    return 0 if result.status == "success" else 1


if __name__ == "__main__":
    sys.exit(main())
