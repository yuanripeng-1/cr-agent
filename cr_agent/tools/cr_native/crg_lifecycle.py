"""
CRG 生命周期管理(PR7)。

CRG 是可选后台能力:默认关闭;开启后构建/更新 MR 工作区 graph,主流程不等待。
本模块不执行 git checkout / fetch;project_root 必须已是待审查源分支代码目录。
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from pathlib import Path

from cr_agent.core.artifacts import append_run_log
from cr_agent.core.agent_config import CrgConfig
from cr_agent.core.review_input import ReviewInput
from cr_agent.tools.cr_native.external_tools import resolve_command
from cr_agent.tools.provider import ToolResult, error_result, ok_result
from cr_agent.utils.logging import get_logger

_logger = get_logger("cr_agent.tools.cr_native.crg")


@dataclass(frozen=True)
class CrgPaths:
    data_dir: Path


@dataclass
class CrgLifecycle:
    config: CrgConfig
    review_input: ReviewInput
    workspace_dir: Path
    result_dir: Path
    config_dir: Path
    command: str | None = None
    paths: CrgPaths | None = None
    task: asyncio.Task[None] | None = None
    started: bool = False
    ready: bool = False
    disabled_reason: str = ""
    warnings: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.command = resolve_command(("code-review-graph",))
        self.paths = plan_crg_paths(
            self.config,
            self.review_input,
            self.workspace_dir,
            self.config_dir,
        )

    def start_background(self) -> None:
        if not self.config.enabled:
            self.disabled_reason = "enabled=false"
            self._log("CRG_DISABLED reason=enabled_false")
            return
        if self.command is None:
            self._degrade("CRG_DISABLED reason=command_missing")
            return
        project_root = Path(self.review_input.project_root)
        if not project_root.is_dir():
            self._degrade(f"CRG_DISABLED reason=project_root_missing path={project_root}")
            return

        self.started = True
        self._log("CRG_BUILD_START")
        self.task = asyncio.create_task(self._build_or_update(project_root))

    async def query(self, query: str) -> ToolResult:
        if not self.config.enabled:
            warning = "crg_query skipped: CRG disabled"
            self._log(f"CRG_QUERY_SKIP reason=disabled query={query}")
            return error_result(warning, warnings=[warning])
        if self.disabled_reason:
            warning = f"crg_query skipped: {self.disabled_reason}"
            self._log(f"CRG_QUERY_SKIP reason={self.disabled_reason} query={query}")
            return error_result(warning, warnings=[warning])

        for attempt in range(1, self.config.max_retry + 1):
            if self.ready:
                return await self._run_query(query)
            self._log(f"CRG_QUERY_WAIT attempt={attempt} max_retry={self.config.max_retry}")
            await asyncio.sleep(self.config.retry_interval_s)

        warning = f"crg_query skipped: graph not ready after {self.config.max_retry} attempts"
        self._log("CRG_QUERY_SKIP reason=not_ready")
        return error_result(warning, warnings=[warning])

    def status(self) -> ToolResult:
        data = {
            "enabled": self.config.enabled,
            "started": self.started,
            "ready": self.ready,
            "disabled_reason": self.disabled_reason,
            "warnings": list(self.warnings),
            "data_dir": str(self.paths.data_dir) if self.paths else "",
        }
        return ok_result(data, list(self.warnings))

    async def _build_or_update(self, project_root: Path) -> None:
        assert self.paths is not None
        try:
            self.paths.data_dir.mkdir(parents=True, exist_ok=True)
            status = await self._run_crg(
                ["status", "--repo", str(project_root), "--data-dir", str(self.paths.data_dir)]
            )
            if status["ok"]:
                self._log("CRG_SCENARIO graph_exists")
                result = await self._run_crg(
                    [
                        "update",
                        "--repo",
                        str(project_root),
                        "--base",
                        self._base_ref(),
                        "--data-dir",
                        str(self.paths.data_dir),
                    ]
                )
            else:
                self._log("CRG_SCENARIO cold_build")
                result = await self._run_crg(
                    ["build", "--repo", str(project_root), "--data-dir", str(self.paths.data_dir)]
                )
            if not result["ok"]:
                self._degrade(str(result["error"]))
                return
            self.ready = True
            self._log(f"CRG_BUILD_DONE data_dir={self.paths.data_dir}")
        except Exception as exc:
            self._degrade(f"CRG_BUILD_ERROR error={exc}")

    async def _run_query(self, query: str) -> ToolResult:
        assert self.paths is not None
        result = await self._run_crg(
            [
                "detect-changes",
                "--repo",
                str(Path(self.review_input.project_root)),
                "--base",
                self._base_ref(),
            ]
        )
        if result["ok"]:
            lines = result["data"]["lines"]
            result["data"] = {
                "query": query,
                "base": self._base_ref(),
                "results": lines,
                "data_dir": str(self.paths.data_dir),
            }
        else:
            self._log(f"CRG_QUERY_SKIP reason=query_failed error={result['error']}")
        return result

    async def _run_crg(self, args: list[str]) -> ToolResult:
        assert self.command is not None
        cmd = [self.command, *args]
        self._log(f"CRG_CMD_START cmd={' '.join(cmd)}")
        try:
            env = os.environ.copy()
            env.setdefault("CRG_SERIAL_PARSE", "1")
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError:
            return error_result("code-review-graph command not available")

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.config.timeout_s
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            warning = f"CRG command timed out after {self.config.timeout_s}s"
            return error_result(warning, warnings=[warning])

        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            warning = f"CRG command failed: {err or f'exit {proc.returncode}'}"
            return error_result(warning, warnings=[warning])
        self._log("CRG_CMD_DONE")
        return ok_result({"stdout": out, "lines": [line for line in out.splitlines() if line]})

    def _degrade(self, warning: str) -> None:
        self.disabled_reason = warning
        self.warnings.append(warning)
        self._log(f"CRG_BUILD_ERROR warning={warning}")
        _logger.warning("DEGRADED reason=CRG warning=%s", warning)

    def _log(self, message: str) -> None:
        append_run_log(self.result_dir, message)
        _logger.info(message)

    def _base_ref(self) -> str:
        return self.review_input.base_sha or self.review_input.start_sha or "HEAD~1"


def plan_crg_paths(
    config: CrgConfig,
    review_input: ReviewInput,
    workspace_dir: Path,
    config_dir: Path,
) -> CrgPaths:
    raw_base = Path(config.base_dir).expanduser()
    base_dir = raw_base if raw_base.is_absolute() else (config_dir / raw_base).resolve()
    project_key = str(review_input.project_id or Path(review_input.project_root).name or "unknown")
    task_key = review_input.task_id or "task"
    return CrgPaths(
        data_dir=base_dir / project_key / _safe_branch_name(task_key),
    )


def _safe_branch_name(branch: str) -> str:
    return branch.replace("/", "__").replace("\\", "__")


def build_crg_lifecycle(
    *,
    config: CrgConfig,
    review_input: ReviewInput,
    workspace_dir: Path,
    result_dir: Path,
    config_dir: Path,
) -> CrgLifecycle:
    return CrgLifecycle(
        config=config,
        review_input=review_input,
        workspace_dir=workspace_dir,
        result_dir=result_dir,
        config_dir=config_dir,
    )
