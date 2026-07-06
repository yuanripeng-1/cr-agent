from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from cr_agent.core.agent_config import (
    VALID_PLATFORMS,
    AgentConfig,
    Platform,
    load_agent_config,
)
from cr_agent.core.sdk_runtime import build_runtime, build_sdk_env
from cr_agent.issue_resolution.contracts import (
    IssueResolutionInput,
    load_issue_resolution_input,
)
from cr_agent.tools.cr_native.crg_lifecycle import CrgLifecycle, build_crg_lifecycle
from cr_agent.tools.cr_native.external_tools import check_external_tool_availability
from cr_agent.tools.cr_native.fs_tools import tool_limits_from_config
from cr_agent.tools.cr_native.git_tools import GitSettings, short_token_hash
from cr_agent.tools.facade import ToolFacade, build_tool_facade_for_platform
from cr_agent.utils.logging import get_logger, install_run_log_handler

_logger = get_logger("cr_agent.issue_resolution.bootstrap")

_DEFAULT_PROJECT_ROOT = "/workspace/project_code"


@dataclass(frozen=True)
class IssueResolutionRuntimeContext:
    config_path: Path
    config: AgentConfig
    context_path: Path
    workspace_dir: Path
    result_dir: Path
    platform: Platform
    resolution_input: IssueResolutionInput
    project_root: Path
    tool_facade: ToolFacade
    resolution_runtime: object
    litellm_gateway: object | None = None


def _record_bootstrap_issue(
    *,
    result_dir: Path,
    config_path: Path,
    context_path: Path,
    reason: str,
) -> None:
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


def _resolve_project_root(config: AgentConfig) -> Path:
    configured = getattr(config.project, "project_root", "") or ""
    if isinstance(configured, str) and configured.strip():
        return Path(configured.strip())
    env_root = os.environ.get("CR_AGENT_PROJECT_ROOT", "").strip()
    if env_root:
        return Path(env_root)
    return Path(_DEFAULT_PROJECT_ROOT)


def _maybe_start_litellm_gateway(config_data: AgentConfig, result_dir: Path):
    llm = config_data.llm
    if not llm.use_litellm_gateway:
        return None

    from cr_agent.core.litellm_gateway import LiteLLMGateway

    gateway = LiteLLMGateway(
        model=llm.model,
        upstream_api_base=llm.api_base,
        upstream_api_key=llm.api_key,
        provider=llm.gateway_provider or "openai",
        port=llm.gateway_port,
        log_path=result_dir / "litellm_gateway.log",
    )
    upstream_base = llm.api_base
    gateway.start()
    llm.api_base = gateway.base_url
    llm.api_key = gateway.api_key
    llm.claude_config_dir = gateway.claude_config_dir
    _logger.info(
        "LITELLM_GATEWAY_CONFIGURED base_url=%s upstream=%s model=%s",
        gateway.base_url,
        upstream_base,
        llm.model,
    )
    return gateway


def bootstrap_issue_resolution(
    config_path: Path,
    platform_override: str | None,
) -> IssueResolutionRuntimeContext:
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    config_data = load_agent_config(config_path)
    config_dir = config_path.parent
    context_path = _resolve_path(config_dir, config_data.context.json_path)

    raw_result_path = config_data.context.result_path
    if raw_result_path:
        result_dir = _resolve_path(config_dir, raw_result_path)
    else:
        fallback_task = "unknown-task"
        result_dir = (config_path.parent / "issue_resolution_result" / fallback_task).resolve()

    if not context_path.exists():
        _record_bootstrap_issue(
            result_dir=result_dir,
            config_path=config_path,
            context_path=context_path,
            reason=f"issue_resolution_context.json not found: {context_path}",
        )
        raise FileNotFoundError(f"issue_resolution_context.json not found: {context_path}")

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
            "1) run_agent_resolution_check.sh --platform <gitlab|infcode>\n"
            "2) [platform] in config.toml"
        )
        _record_bootstrap_issue(
            result_dir=result_dir,
            config_path=config_path,
            context_path=context_path,
            reason=message,
        )
        raise ValueError(message)

    resolution_input = load_issue_resolution_input(context_path)
    project_root = _resolve_project_root(config_data)

    install_run_log_handler(result_dir)
    _logger.info(
        "ISSUE_RESOLUTION_BOOTSTRAP task_id=%s config=%s context=%s result_dir=%s project_root=%s",
        resolution_input.task_id,
        config_path,
        context_path,
        result_dir,
        project_root,
    )
    _logger.info(
        "ISSUE_RESOLUTION_PLATFORM task_id=%s platform=%s issues=%s",
        resolution_input.task_id,
        final_platform,
        len(resolution_input.issues),
    )

    workspace_dir = context_path.parent
    git_token = (config_data.git.token or "").strip()
    git_settings = GitSettings(
        token=git_token,
        timeout_s=config_data.git.timeout_s,
        allow_network=config_data.git.allow_network,
    )
    check_external_tool_availability()
    _logger.info(
        "GIT_TOKEN_RESOLVED configured=%s source=config hash=%s allow_network=%s",
        bool(git_token),
        short_token_hash(git_token),
        config_data.git.allow_network,
    )

    litellm_gateway = _maybe_start_litellm_gateway(config_data, result_dir)

    review_input_stub = SimpleNamespace(
        project_root=str(project_root),
        project_id=resolution_input.project_id,
        mr_iid=resolution_input.mr_iid,
        source_branch=resolution_input.source_branch,
        target_branch=resolution_input.target_branch,
        base_sha="",
        head_sha=resolution_input.merged_commit_sha,
        task_id=resolution_input.task_id,
    )
    crg_lifecycle = build_crg_lifecycle(
        config=config_data.tools.crg,
        review_input=review_input_stub,  # type: ignore[arg-type]
        workspace_dir=workspace_dir,
        result_dir=result_dir,
        config_dir=config_dir,
    )

    tool_facade = build_tool_facade_for_platform(
        final_platform,
        project_root=project_root,
        limits=tool_limits_from_config(config_data),
        git_settings=git_settings,
        crg_lifecycle=crg_lifecycle,
    )
    resolution_runtime = build_runtime(config_data)

    sdk_env = build_sdk_env(config_data.llm)
    os.environ.update(sdk_env)
    _logger.info(
        "MODEL_GATEWAY_CONFIGURED base_url_set=%s api_key_configured=%s",
        "ANTHROPIC_BASE_URL" in sdk_env,
        "ANTHROPIC_API_KEY" in sdk_env,
    )
    os.environ["CR_AGENT_CONFIG"] = str(config_path)
    os.environ["CR_AGENT_ISSUE_RESOLUTION_CONTEXT"] = str(context_path)

    return IssueResolutionRuntimeContext(
        config_path=config_path,
        config=config_data,
        context_path=context_path,
        workspace_dir=workspace_dir,
        result_dir=result_dir,
        platform=final_platform,
        resolution_input=resolution_input,
        project_root=project_root,
        tool_facade=tool_facade,
        resolution_runtime=resolution_runtime,
        litellm_gateway=litellm_gateway,
    )
