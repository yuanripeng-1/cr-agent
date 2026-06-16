from __future__ import annotations

import os
import uuid
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
from cr_agent.core.sdk_runtime import build_runtime, build_sdk_env
from cr_agent.tools.cr_native.crg_lifecycle import CrgLifecycle, build_crg_lifecycle
from cr_agent.tools.cr_native.external_tools import check_external_tool_availability
from cr_agent.tools.cr_native.git_tools import GitSettings, resolve_git_token, short_token_hash
from cr_agent.tools.facade import ToolFacade, build_tool_facade_for_platform
from cr_agent.utils.logging import get_logger, install_run_log_handler

_logger = get_logger("cr_agent.bootstrap")


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
    # CRG 生命周期状态;默认 disabled,由 run_review 在主流程开始时启动后台任务。
    crg_lifecycle: CrgLifecycle
    # 子 agent runtime;summary skill 通过它调用 summary subagent。
    summary_runtime: object
    # 子 agent runtime;collect_context skill 通过它调用 context subagent。
    context_runtime: object
    # 子 agent runtime;dimension_review skill 通过它逐维调用 dimension subagent。
    dimension_runtime: object
    # 贯穿本次审查运行的 trace id。
    trace_id: str


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

    trace_id = uuid.uuid4().hex[:12]
    install_run_log_handler(result_dir)
    _logger.info(
        "BOOTSTRAP_CONFIG_LOADED trace_id=%s config=%s context=%s result_dir=%s",
        trace_id,
        config_path,
        context_path,
        result_dir,
    )
    _logger.info("PLATFORM_SELECTED trace_id=%s platform=%s", trace_id, final_platform)

    workspace_dir = context_path.parent

    # 解析 git token(context.git_token > config.git.token > "")并构造 GitSettings。
    # 日志只记 configured/source/短 hash,绝不打印明文(并经脱敏 Filter 兜底)。
    git_token, git_token_source = resolve_git_token(context_data, config_data.git)
    git_settings = GitSettings(
        token=git_token,
        timeout_s=config_data.git.timeout_s,
        allow_network=config_data.git.allow_network,
    )
    check_external_tool_availability()
    _logger.info(
        "GIT_TOKEN_RESOLVED configured=%s source=%s hash=%s allow_network=%s",
        bool(git_token),
        git_token_source,
        short_token_hash(git_token),
        config_data.git.allow_network,
    )

    crg_lifecycle = build_crg_lifecycle(
        config=config_data.tools.crg,
        review_input=context_data,
        workspace_dir=workspace_dir,
        result_dir=result_dir,
        config_dir=config_dir,
    )

    # 平台确定后选定工具 provider 并加载 allowlist;provider 选择与 allowlist
    # 结果在 facade 内部记日志(TOOL_PROVIDER_SELECTED / TOOL_ALLOWLIST_LOADED)。
    # project_root 注入给 cr-native 文件工具;git_settings 注入给 git 工具。
    tool_facade = build_tool_facade_for_platform(
        final_platform,
        project_root=Path(context_data.project_root),
        git_settings=git_settings,
        crg_lifecycle=crg_lifecycle,
    )
    summary_runtime = build_runtime(config_data)
    context_runtime = build_runtime(config_data)
    dimension_runtime = build_runtime(config_data)

    # 早期把 LiteLLM 网关 env 写入进程环境,供 SDK/claude CLI 启动时读取。
    # 空值不注入;日志只记布尔,api_key 永不出现明文(并经脱敏 Filter 兜底)。
    sdk_env = build_sdk_env(config_data.llm)
    os.environ.update(sdk_env)
    _logger.info(
        "MODEL_GATEWAY_CONFIGURED base_url_set=%s api_key_configured=%s",
        "ANTHROPIC_BASE_URL" in sdk_env,
        "ANTHROPIC_API_KEY" in sdk_env,
    )

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
        crg_lifecycle=crg_lifecycle,
        summary_runtime=summary_runtime,
        context_runtime=context_runtime,
        dimension_runtime=dimension_runtime,
        trace_id=trace_id,
    )
