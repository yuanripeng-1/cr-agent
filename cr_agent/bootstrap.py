"""
bootstrap:一次审查运行的"装配层"。

"bootstrap"(引导/装配)指在真正跑审查前,把运行所需的一切准备齐:
加载并校验 agent_config.toml 与 context.json、确定平台、解析 git token、
按需起本地 LiteLLM 网关、按平台选定工具 facade、构造主/子 agent runtime,
最后聚合成一个不可变的 RuntimeContext 交给 orchestrator。

RuntimeContext = 这次运行的"上下文快照":把上面装配出的配置、路径、平台、
工具 facade、各 runtime、网关句柄等集中持有,后续流程只读取它,不再各自加载。
"""

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
    # 可选:本地 LiteLLM proxy 网关句柄(use_litellm_gateway=true 时存在),
    # 由 main.py 在运行结束后关闭;atexit 兜底。
    litellm_gateway: object | None = None


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
    """
    执行装配并返回 RuntimeContext。

    这里的 "runtime" 指承载一次审查运行所需依赖的执行环境:既包括 RuntimeContext
    聚合的配置/路径/平台/工具,也包括其中的主/子 agent runtime(对模型调用的适配器)。
    bootstrap_runtime 只负责把它们准备好,不发起任何模型调用。
    """
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

    install_run_log_handler(result_dir)
    _logger.info(
        "BOOTSTRAP_CONFIG_LOADED task_id=%s config=%s context=%s result_dir=%s",
        context_data.task_id,
        config_path,
        context_path,
        result_dir,
    )
    _logger.info(
        "PLATFORM_SELECTED task_id=%s platform=%s",
        context_data.task_id,
        final_platform,
    )

    workspace_dir = context_path.parent

    # 解析 git token(context.git_token > config.git.token > "")并构造 GitSettings。
    # 日志只记 configured/source/短 hash,绝不打印明文(并经脱敏 Filter 兜底)。
    git_token, git_token_source = resolve_git_token(context_data, config_data.git)
    git_settings = GitSettings(
        token=git_token,
        timeout_s=config_data.git.timeout_s,
        allow_network=config_data.git.allow_network,
    )
    # 启动期只取其副作用:打 EXTERNAL_TOOL_AVAILABLE/MISSING 日志,便于排查环境。
    # 返回的状态 dict 供单测与诊断脚本消费,此处无需消费,故不接收返回值。
    check_external_tool_availability()
    _logger.info(
        "GIT_TOKEN_RESOLVED configured=%s source=%s hash=%s allow_network=%s",
        bool(git_token),
        git_token_source,
        short_token_hash(git_token),
        config_data.git.allow_network,
    )

    # 方案 B:按需在本机起 LiteLLM proxy 作为 Anthropic 兼容网关,并把 [llm] 的
    # api_base/api_key 改写为本地 proxy。改写发生在 build_runtime / build_sdk_env 之前,
    # 因此主/子 agent 与早期 env 都会自然指向网关。upstream 仍记录在 proxy 内部路由。
    litellm_gateway = _maybe_start_litellm_gateway(config_data, result_dir)

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
        litellm_gateway=litellm_gateway,
    )


def _maybe_start_litellm_gateway(config_data: AgentConfig, result_dir: Path):
    """
    若 [llm].use_litellm_gateway 为真,启动本地 LiteLLM proxy 并把 [llm] 的
    api_base/api_key 改写为本地 proxy 地址 / master_key。返回网关句柄(供关闭),
    未开启时返回 None。

    注意:此处原地改写 config_data.llm(pydantic, extra=allow, 非 frozen),
    使后续 build_runtime / build_main_agent_runtime / build_sdk_env 自然走网关。
    """
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
    # 改写为本地网关入口;upstream 仍由 proxy 内部路由,真实 key 不再出现在 env。
    llm.api_base = gateway.base_url
    llm.api_key = gateway.api_key
    # 隔离 claude CLI 配置目录,防止宿主 ~/.claude/settings.json 覆盖网关地址。
    llm.claude_config_dir = gateway.claude_config_dir
    _logger.info(
        "LITELLM_GATEWAY_CONFIGURED base_url=%s upstream=%s model=%s",
        gateway.base_url,
        upstream_base,
        llm.model,
    )
    return gateway
