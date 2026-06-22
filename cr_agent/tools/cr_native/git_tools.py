"""
cr-native git 工具(PR5)。

实现最小**只读** git 工具(git_status / git_rev_parse),并把 git_fetch / git_checkout
做成**受控接口**:默认不联网、不切分支。git/token/远程权限缺失一律降级,不让任务失败。

=== 分支不变量(与任务文档一致,这里在代码层落实) ===
CRG 后台构建启动前,工作区必须已处于**待审查分支(即 source_branch)**;分支切换只能由
上游或显式 git 步骤完成,**不得隐式切分支**。因此 git_checkout 在本 PR 默认拒绝执行,
避免后台构建期间工作区被切走的竞态(见 PR7)。

token 解析优先级:context.git_token > config.git.token > ""。
日志只记 configured=true/false 或短 hash,绝不打印明文(并经 PR1 脱敏 Filter 兜底)。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cr_agent.tools.cr_native.fs_tools import _guard, _require_root
from cr_agent.tools.provider import ToolHandler, ToolResult, error_result, ok_result


@dataclass(frozen=True)
class GitSettings:
    # 解析后的 git token(可能为空)。
    token: str = ""
    # git 命令超时(秒)。
    timeout_s: float = 30.0
    # 是否允许联网(git_fetch 等远程操作);默认关闭。
    allow_network: bool = False


def resolve_git_token(review_input: Any, git_config: Any) -> tuple[str, str]:
    """
    解析 git token,优先级:context.git_token > config.git.token > ""。
    返回 (token, source),source ∈ {"context", "config", "none"}。
    """
    context_token = (getattr(review_input, "git_token", "") or "").strip()
    if context_token:
        return context_token, "context"
    config_token = (getattr(git_config, "token", "") or "").strip()
    if config_token:
        return config_token, "config"
    return "", "none"


def short_token_hash(token: str) -> str:
    """有 token 时返回 sha256 前 8 位短 hash,否则 "-"。用于日志,绝不暴露明文。"""
    if not token:
        return "-"
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


async def _run_git(
    args: list[str],
    *,
    cwd: Path,
    timeout_s: float,
    tool_name: str,
    extra_env: dict[str, str] | None = None,
) -> ToolResult:
    """执行一条 git 命令并结构化返回。git 缺失/非零退出/超时都降级,不抛穿。

    extra_env 用于注入鉴权等敏感配置(如 http.extraHeader),走环境变量而非 argv,
    避免 token 出现在进程命令行(防 `ps` 泄露)。
    """

    env = None
    if extra_env:
        env = os.environ.copy()
        env.update(extra_env)

    async def work() -> ToolResult:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                *args,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
        except FileNotFoundError:
            return error_result("git not available")

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return error_result(f"{tool_name} timed out after {timeout_s}s")

        out = stdout.decode("utf-8", errors="replace")
        err = stderr.decode("utf-8", errors="replace").strip()
        if proc.returncode != 0:
            # 非零退出(非 repo、权限不足等)→ 降级,不抛穿。
            return error_result(f"{tool_name} failed: {err or f'exit {proc.returncode}'}")
        return ok_result({"stdout": out})

    return await _guard(work(), tool_name=tool_name, timeout_s=timeout_s)


def make_git_status(project_root: Path | None, gs: GitSettings) -> ToolHandler:
    async def handler(args: dict) -> ToolResult:
        guard = _require_root(project_root, "git_status")
        if guard is not None:
            return guard
        return await _run_git(
            ["status", "--porcelain"],
            cwd=project_root,
            timeout_s=gs.timeout_s,
            tool_name="git_status",
        )

    return handler


def make_git_rev_parse(project_root: Path | None, gs: GitSettings) -> ToolHandler:
    async def handler(args: dict) -> ToolResult:
        guard = _require_root(project_root, "git_rev_parse")
        if guard is not None:
            return guard
        ref = str(args.get("ref") or "HEAD")
        # 只接受单个 ref,避免注入额外 git 参数。
        if ref.startswith("-"):
            return error_result("invalid ref")
        return await _run_git(
            ["rev-parse", ref],
            cwd=project_root,
            timeout_s=gs.timeout_s,
            tool_name="git_rev_parse",
        )

    return handler


def make_git_fetch(project_root: Path | None, gs: GitSettings) -> ToolHandler:
    async def handler(args: dict) -> ToolResult:
        guard = _require_root(project_root, "git_fetch")
        if guard is not None:
            return guard
        # 受控接口:默认不联网。
        if not gs.allow_network:
            return error_result("network disabled: git_fetch skipped (allow_network=false)")
        remote = str(args.get("remote") or "origin")
        if remote.startswith("-"):
            return error_result("invalid remote")
        cmd = ["fetch", remote]
        ref = args.get("ref")
        if ref:
            if str(ref).startswith("-"):
                return error_result("invalid ref")
            cmd.append(str(ref))
        # token 非空时,经环境变量注入 http.extraHeader 鉴权(不进 argv,防 ps 泄露),
        # 否则私有仓库 fetch 必定 Authentication failed。GIT_TERMINAL_PROMPT=0 防交互挂起。
        extra_env: dict[str, str] | None = None
        if gs.token:
            extra_env = {
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "http.extraHeader",
                "GIT_CONFIG_VALUE_0": f"Authorization: Bearer {gs.token}",
            }
        # 远程权限不足/失败由 _run_git 的非零退出转成降级。
        return await _run_git(
            cmd,
            cwd=project_root,
            timeout_s=gs.timeout_s,
            tool_name="git_fetch",
            extra_env=extra_env,
        )

    return handler


def make_git_checkout(project_root: Path | None, gs: GitSettings) -> ToolHandler:
    async def handler(args: dict) -> ToolResult:
        # 受控接口:落实分支不变量,默认拒绝,绝不隐式切分支。
        return error_result(
            "implicit branch switch refused: git_checkout is owned by upstream "
            "or an explicit git step (see branch invariant)"
        )

    return handler
