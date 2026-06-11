from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import toml

from cr_agent.core.review_input import ReviewInput, load_review_input


VALID_PLATFORMS = {"gitlab", "infcode"}


@dataclass(frozen=True)
class RuntimeContext:
    config_path: Path
    context_path: Path
    workspace_dir: Path
    result_dir: Path
    platform: str
    review_input: ReviewInput


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


def _require_platform(value: str | None, source: str) -> str | None:
    if value is None or value == "":
        return None
    normalized = value.strip().lower()
    if normalized not in VALID_PLATFORMS:
        raise ValueError(
            f"Invalid platform from {source}: {value}. "
            f"Expected one of {sorted(VALID_PLATFORMS)}."
        )
    return normalized


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

    config_data: dict[str, Any] = toml.load(config_path)
    context_cfg = config_data.get("context", {})
    if "json_path" not in context_cfg:
        raise ValueError("Missing [context].json_path in config.toml")

    config_dir = config_path.parent
    context_path = _resolve_path(config_dir, context_cfg["json_path"])
    if not context_path.exists():
        raise FileNotFoundError(f"context.json not found: {context_path}")

    context_data = load_review_input(context_path)
    raw_result_path = context_cfg.get("result_path", "")
    if raw_result_path:
        result_dir = _resolve_path(config_dir, raw_result_path)
    else:
        fallback_task = context_data.task_id or "unknown-task"
        result_dir = (config_path.parent / "workspace" / fallback_task / "cr_result").resolve()

    try:
        override_platform = _require_platform(platform_override, "--platform")
        config_platform_raw = config_data.get("platform")
        if not config_platform_raw:
            config_platform_raw = config_data.get("llm", {}).get("platform")
        config_platform = _require_platform(config_platform_raw, "config.toml platform")
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

    # 保留只读追踪环境变量，避免引入启动判定分支。
    import os
    os.environ["CR_AGENT_CONFIG"] = str(config_path)
    os.environ["CR_AGENT_CONTEXT"] = str(context_path)

    return RuntimeContext(
        config_path=config_path,
        context_path=context_path,
        workspace_dir=workspace_dir,
        result_dir=result_dir,
        platform=final_platform,
        review_input=context_data,
    )

