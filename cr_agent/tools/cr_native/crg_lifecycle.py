"""
CRG 生命周期管理(PR7)。

CRG 是可选后台能力:默认关闭;开启后构建/更新 MR 工作区 graph,主流程不等待。
本模块不执行 git checkout / fetch;project_root 必须已是待审查源分支代码目录。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from cr_agent.core.artifacts import append_run_log
from cr_agent.core.agent_config import CrgConfig
from cr_agent.core.review_input import ReviewInput
from cr_agent.tools.cr_native.external_tools import resolve_command
from cr_agent.tools.provider import ToolResult, error_result, ok_result
from cr_agent.utils.logging import get_logger

_logger = get_logger("cr_agent.tools.cr_native.crg")

# 子 agent 调用图工具默认返回上限:控制喂给 LLM 的上下文体积。
_DEFAULT_GRAPH_LIMIT = 20

# 在 crg 环境 python 子进程里执行的固定脚本。
# 调用规格通过环境变量 CRG_CALL(JSON) 传入,避免把用户输入拼进源码(防注入);
# 图数据目录通过 CRG_DATA_DIR 传入。任何异常都降级为 {"status":"error"},不抛栈。
_CRG_PY_SCRIPT = r'''
import json, os, sys

spec = json.loads(os.environ.get("CRG_CALL", "{}"))
func = spec.get("func")
kwargs = dict(spec.get("kwargs") or {})
limit = int(spec.get("limit") or 20)

try:
    if func in ("callers_of", "callees_of"):
        from code_review_graph.tools.query import query_graph
        target = kwargs.pop("target", "")
        result = query_graph(func, target, detail_level="minimal", **kwargs)
    elif func == "affected_flows":
        from code_review_graph.tools.review import get_affected_flows_func
        result = get_affected_flows_func(**kwargs)
    elif func == "get_flow":
        from code_review_graph.tools.flows_tools import get_flow
        result = get_flow(**kwargs)
    else:
        print(json.dumps({"status": "error", "error": "unknown func " + str(func)}))
        sys.exit(0)
except Exception as exc:  # 降级而非崩溃
    print(json.dumps({"status": "error", "error": str(exc)}))
    sys.exit(0)


def _trim_steps(flow, keep):
    # flow 的 steps 是逐节点完整明细(体积大);path 已是紧凑调用序列。
    if isinstance(flow, dict) and isinstance(flow.get("steps"), list):
        steps = flow["steps"]
        if len(steps) > keep:
            flow["steps"] = steps[:keep]
            flow["steps_truncated_to"] = keep


# 针对 flow 结果做体积裁剪:affected_flows 概览只保留紧凑 path,丢弃逐步 steps;
# get_flow 需要路径细节,保留 path 并把 steps 截断到 limit。
if isinstance(result, dict):
    if func == "affected_flows":
        for flow in result.get("affected_flows", []) or []:
            if isinstance(flow, dict):
                flow.pop("steps", None)
    elif func == "get_flow":
        _trim_steps(result.get("flow"), limit)

    # 顶层 list 字段按 limit 截断,进一步压缩上下文。
    for key, value in list(result.items()):
        if isinstance(value, list) and len(value) > limit:
            result[key] = value[:limit]
            result[key + "_truncated_to"] = limit

print(json.dumps(result, default=str, ensure_ascii=False))
'''


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

    async def callers(self, target: str, limit: int = _DEFAULT_GRAPH_LIMIT) -> ToolResult:
        return await self._graph_call("crg_callers", "callers_of", {"target": target}, limit)

    async def callees(self, target: str, limit: int = _DEFAULT_GRAPH_LIMIT) -> ToolResult:
        return await self._graph_call("crg_callees", "callees_of", {"target": target}, limit)

    async def affected_flows(
        self, base: str | None = None, limit: int = _DEFAULT_GRAPH_LIMIT
    ) -> ToolResult:
        return await self._graph_call(
            "crg_affected_flows", "affected_flows", {"base": base or self._base_ref()}, limit
        )

    async def get_flow(
        self,
        flow_name: str | None = None,
        flow_id: int | None = None,
        limit: int = _DEFAULT_GRAPH_LIMIT,
    ) -> ToolResult:
        kwargs: dict[str, object] = {}
        if flow_name:
            kwargs["flow_name"] = flow_name
        if flow_id is not None:
            kwargs["flow_id"] = flow_id
        return await self._graph_call("crg_get_flow", "get_flow", kwargs, limit)

    async def _graph_call(
        self, label: str, func: str, kwargs: dict[str, object], limit: int
    ) -> ToolResult:
        if not self.config.enabled:
            warning = f"{label} skipped: CRG disabled"
            self._log(f"CRG_QUERY_SKIP reason=disabled query={label}")
            return error_result(warning, warnings=[warning])
        if self.disabled_reason:
            warning = f"{label} skipped: {self.disabled_reason}"
            self._log(f"CRG_QUERY_SKIP reason={self.disabled_reason} query={label}")
            return error_result(warning, warnings=[warning])

        for attempt in range(1, self.config.max_retry + 1):
            if self.ready:
                return await self._run_crg_python(func, kwargs, limit)
            self._log(f"CRG_QUERY_WAIT attempt={attempt} max_retry={self.config.max_retry}")
            await asyncio.sleep(self.config.retry_interval_s)

        warning = f"{label} skipped: graph not ready after {self.config.max_retry} attempts"
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

    async def build_or_update(self) -> ToolResult:
        """供 crg_build_or_update 工具调用；与 start_background 幂等。"""
        if not self.config.enabled:
            warning = "crg_build_or_update skipped: CRG disabled"
            self._log("CRG_BUILD_SKIP reason=disabled")
            return error_result(warning, warnings=[warning])
        if self.disabled_reason:
            warning = f"crg_build_or_update skipped: {self.disabled_reason}"
            self._log(f"CRG_BUILD_SKIP reason={self.disabled_reason}")
            return error_result(warning, warnings=list(self.warnings))
        if self.command is None:
            warning = "CRG_DISABLED reason=command_missing"
            return error_result(warning, warnings=[warning])
        project_root = Path(self.review_input.project_root)
        if not project_root.is_dir():
            warning = f"CRG_DISABLED reason=project_root_missing path={project_root}"
            return error_result(warning, warnings=[warning])

        if self.ready:
            status = self.status()
            data = dict(status["data"] or {})
            data["already_ready"] = True
            return ok_result(data, list(self.warnings))

        if self.task is not None and not self.task.done():
            await self.task
        elif not self.started:
            self.started = True
            self._log("CRG_BUILD_START")
            await self._build_or_update(project_root)
        elif self.task is not None:
            await self.task

        if self.ready:
            assert self.paths is not None
            return ok_result(
                {
                    "ready": True,
                    "data_dir": str(self.paths.data_dir),
                    "warnings": list(self.warnings),
                },
                list(self.warnings),
            )

        warning = self.disabled_reason or "crg build_or_update failed"
        return error_result(warning, warnings=list(self.warnings))

    async def _build_or_update(self, project_root: Path) -> None:
        assert self.paths is not None
        try:
            self.paths.data_dir.mkdir(parents=True, exist_ok=True)
            status = await self._run_crg(
                ["status", "--repo", str(project_root), "--data-dir", str(self.paths.data_dir)]
            )
            # status 即使图为空也会创建空 db 并返回 0,不能只看退出码:
            # 必须从输出判断图是否已构建(Nodes:0 / Last updated:never 视为未构建),
            # 否则对空图执行增量 update 不会产生任何节点,图永远是空的。
            if status["ok"] and _graph_is_populated(str(status["data"].get("stdout", ""))):
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

    async def _run_crg_python(self, func: str, kwargs: dict[str, object], limit: int) -> ToolResult:
        assert self.paths is not None
        python = self._crg_python()
        if python is None:
            warning = "crg python env not found (set CRG_PYTHON or tools.crg.python_path)"
            self._log("CRG_PY_SKIP reason=python_missing")
            return error_result(warning, warnings=[warning])

        repo_root = str(Path(self.review_input.project_root).resolve())
        spec = {"func": func, "kwargs": {**kwargs, "repo_root": repo_root}, "limit": limit}
        env = os.environ.copy()
        env["CRG_DATA_DIR"] = str(self.paths.data_dir)
        env["CRG_CALL"] = json.dumps(spec)

        self._log(f"CRG_PY_START func={func} kwargs={kwargs} limit={limit}")
        try:
            proc = await asyncio.create_subprocess_exec(
                python,
                "-c",
                _CRG_PY_SCRIPT,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError:
            return error_result("crg python env not available")

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=self.config.timeout_s
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            warning = f"crg python call timed out after {self.config.timeout_s}s"
            return error_result(warning, warnings=[warning])

        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            warning = f"crg python call failed: {err or f'exit {proc.returncode}'}"
            self._log(f"CRG_PY_FAIL func={func} error={warning}")
            return error_result(warning, warnings=[warning])

        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            warning = "crg python call returned non-JSON output"
            self._log(f"CRG_PY_FAIL func={func} reason=bad_json")
            return error_result(warning, warnings=[warning])

        if isinstance(data, dict) and data.get("status") == "error":
            warning = f"crg python call error: {data.get('error', 'unknown')}"
            self._log(f"CRG_PY_FAIL func={func} error={warning}")
            return error_result(warning, warnings=[warning])

        self._log(f"CRG_PY_DONE func={func}")
        return ok_result(data)

    def _crg_python(self) -> str | None:
        override = os.environ.get("CRG_PYTHON") or str(getattr(self.config, "python_path", "") or "")
        if override and Path(override).exists():
            return override
        cli = self._real_crg_cli()
        if cli is None:
            return None
        candidate = Path(cli).parent / "python"
        return str(candidate) if candidate.exists() else None

    def _real_crg_cli(self) -> str | None:
        # self.command 通常是裸命令名,需经 PATH 解析为绝对路径;它可能是
        # .tools/bin 下的 bash wrapper(exec "<真实 cli>"),解析出真实 cli 路径
        # 以推断同目录的 crg 环境 python;否则按解析后的路径处理。
        if not self.command:
            return None
        path = Path(shutil.which(self.command) or self.command)
        try:
            if path.is_file():
                text = path.read_text(encoding="utf-8", errors="replace")
                match = re.search(r'exec\s+"([^"]+)"', text)
                if match:
                    return match.group(1)
        except OSError:
            pass
        return str(path)

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


def _graph_is_populated(status_stdout: str) -> bool:
    """
    根据 `code-review-graph status` 输出判断图是否已真正构建。

    status 对空/未构建的图也会返回 0,并打印 `Nodes: 0` 与 `Last updated: never`。
    只有节点数 > 0 时才视为已构建,可走增量 update;否则需要冷启动 build。
    """
    text = status_stdout.lower()
    if "last updated: never" in text:
        return False
    match = re.search(r"nodes:\s*(\d+)", text)
    if match:
        return int(match.group(1)) > 0
    # 输出格式不符合预期时保守起见当作未构建,触发全量 build。
    return False


def plan_crg_paths(
    config: CrgConfig,
    review_input: ReviewInput,
    workspace_dir: Path,
    config_dir: Path,
) -> CrgPaths:
    raw_base = Path(config.base_dir).expanduser()
    base_dir = raw_base if raw_base.is_absolute() else (workspace_dir.parent / raw_base).resolve()
    project_key = str(review_input.project_id or Path(review_input.project_root).name or "unknown")
    project_key = _safe_branch_name(project_key)
    if review_input.mr_iid is not None:
        lineage_dir = Path("mrs") / str(review_input.mr_iid)
    elif review_input.source_branch:
        lineage_dir = Path("branches") / _safe_branch_name(review_input.source_branch)
    else:
        lineage_dir = Path("default")
    return CrgPaths(
        data_dir=base_dir / "projects" / project_key / lineage_dir,
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
