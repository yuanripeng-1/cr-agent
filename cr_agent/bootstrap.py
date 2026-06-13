from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from cr_agent.core.agent_config import (
    VALID_PLATFORMS,
    AgentConfig,
    Platform,
    load_agent_config,
)
from cr_agent.core.review_input import ReviewInput, load_review_input
from cr_agent.tools.facade import ToolFacade, build_tool_facade_for_platform


@dataclass(frozen=True)
class RuntimeContext:
    # 当前运行使用的 agent 配置文件路径。
    config_path: Path
    # 解析并校验后的 agent 配置对象。
    config: AgentConfig
    # 当前运行使用的 context.json 路径。
    context_path: Path
    # 当前任务的工作区目录。
    workspace_dir: Path
    # 当前任务的审查结果输出目录。
    result_dir: Path
    # 本次运行最终确定的平台类型。
    platform: Platform
    # 解析并校验后的 code review 输入对象。
    review_input: ReviewInput
    # 按平台选定 provider 并加载 allowlist 后的工具外观层。
    tool_facade: ToolFacade


def _record_bootstrap_issue(
    *,
    result_dir: Path,
    config_path: Path,
    context_path: Path,
    reason: str,
) -> None:
    """
    在启动早期失败时写入 run.log，便于排查与用户感知。
    """
    try:
        result_dir.mkdir(parents=True, exist_ok=True)
        log_path = result_dir / "run.log"
        timestamp = datetime.now().isoformat(timespec="seconds")
        log_path.write_text(
            (
                f"[{timestamp}] [bootstrap_error] {reason}\n"
                f"config_path={config_path}\n"
                f"context_path={context_path}\n"
            ),
            encoding="utf-8",
        )
    except Exception:
        # 启动失败记录不应该反向阻塞主异常抛出。
        pass


def _require_platform(value: str | None, source: str) -> Platform | None:
    if value is None or value == "":
        return None
    normalized = value.strip().lower()
    if normalized not in VALID_PLATFORMS:
        raise ValueError(
            f"Invalid platform from {source}: {value}. "
            f"Expected one of {sorted(VALID_PLATFORMS)}."
        )
    return cast(Platform, normalized)


def _resolve_path(base_dir: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    if path.is_absolute():
        return path

    base_candidate = (base_dir / path).resolve()
    if base_candidate.exists():
        return base_candidate

    cwd_candidate = (Path.cwd() / path).resolve()
    if cwd_candidate.exists():
        return cwd_candidate

    return base_candidate


def bootstrap_runtime(config_path: Path, platform_override: str | None) -> RuntimeContext:
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    config_data = load_agent_config(config_path)

    config_dir = config_path.parent
    context_path = _resolve_path(config_dir, config_data.context.json_path)
    if not context_path.exists():
        raise FileNotFoundError(f"context.json not found: {context_path}")

    context_data = load_review_input(context_path)
    raw_result_path = config_data.context.result_path
    if raw_result_path:
        result_dir = _resolve_path(config_dir, raw_result_path)
    else:
        fallback_task = context_data.task_id or "unknown-task"
        result_dir = (config_path.parent / "workspace" / fallback_task / "cr_result").resolve()

    try:
        override_platform = _require_platform(platform_override, "--platform")
        config_platform = config_data.configured_platform()
        final_platform = override_platform or config_platform
    except ValueError as exc:
        _record_bootstrap_issue(
            result_dir=result_dir,
            config_path=config_path,
            context_path=context_path,
            reason=str(exc),
        )
        raise

    if not final_platform:
        message = (
            "Platform is required but missing. Provide one of:\n"
            "1) RUN.sh --platform <gitlab|infcode>\n"
            "2) [platform] in config.toml"
        )
        _record_bootstrap_issue(
            result_dir=result_dir,
            config_path=config_path,
            context_path=context_path,
            reason=message,
        )
        raise ValueError(message)

    workspace_dir = context_path.parent

    # 平台确定后选定工具 provider 并加载 allowlist;provider 选择与 allowlist
    # 结果在 facade 内部记日志(TOOL_PROVIDER_SELECTED / TOOL_ALLOWLIST_LOADED)。
    tool_facade = build_tool_facade_for_platform(final_platform)

    # 保留只读追踪环境变量，避免引入启动判定分支。
    os.environ["CR_AGENT_CONFIG"] = str(config_path)
    os.environ["CR_AGENT_CONTEXT"] = str(context_path)

    return RuntimeContext(
        config_path=config_path,
        config=config_data,
        context_path=context_path,
        workspace_dir=workspace_dir,
        result_dir=result_dir,
        platform=final_platform,
        review_input=context_data,
        tool_facade=tool_facade,
    )
