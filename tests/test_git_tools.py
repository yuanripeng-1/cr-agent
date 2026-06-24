from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from cr_agent.tools.cr_native import git_tools
from cr_agent.tools.cr_native.git_tools import (
    GitSettings,
    make_git_checkout,
    make_git_fetch,
    make_git_rev_parse,
    make_git_status,
    resolve_git_token,
    short_token_hash,
)
from cr_agent.utils.logging import REDACTED, get_logger

_HAS_GIT = shutil.which("git") is not None
_requires_git = pytest.mark.skipif(not _HAS_GIT, reason="git executable not on PATH")


# ----- token 优先级 -----

def test_token_priority_context_wins() -> None:
    ri = SimpleNamespace(git_token="ctx-secret")
    gc = SimpleNamespace(token="cfg-secret")
    token, source = resolve_git_token(ri, gc)
    assert token == "ctx-secret"
    assert source == "context"


def test_token_priority_falls_back_to_config() -> None:
    ri = SimpleNamespace(git_token="")
    gc = SimpleNamespace(token="cfg-secret")
    token, source = resolve_git_token(ri, gc)
    assert token == "cfg-secret"
    assert source == "config"


def test_token_priority_none_when_both_empty() -> None:
    ri = SimpleNamespace(git_token="")
    gc = SimpleNamespace(token="")
    token, source = resolve_git_token(ri, gc)
    assert token == ""
    assert source == "none"


# ----- 日志无 token 明文 -----

def test_short_token_hash_hides_plaintext() -> None:
    h = short_token_hash("ghp_TOPSECRET_123")
    assert "ghp_TOPSECRET_123" not in h
    assert len(h) == 8
    assert short_token_hash("") == "-"


def test_git_token_log_line_has_no_plaintext(caplog) -> None:
    logger = get_logger("cr_agent.tests.gittoken")
    logger.setLevel(logging.INFO)
    token = "ghp_TOPSECRET_123"
    with caplog.at_level(logging.INFO, logger="cr_agent.tests.gittoken"):
        # 模拟 bootstrap 的 GIT_TOKEN_RESOLVED 行 + 一个含明文的恶意行。
        logger.info(
            "GIT_TOKEN_RESOLVED configured=%s source=%s hash=%s",
            bool(token),
            "context",
            short_token_hash(token),
        )
        logger.info("debug git_token=%s", token)
    rendered = "\n".join(r.getMessage() for r in caplog.records)
    assert token not in rendered
    assert REDACTED in rendered


# ----- 降级:git 缺失 -----

@pytest.mark.asyncio
async def test_git_status_degrades_when_git_missing(tmp_path: Path, monkeypatch) -> None:
    async def _raise(*args, **kwargs):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(git_tools.asyncio, "create_subprocess_exec", _raise)
    result = await make_git_status(tmp_path, GitSettings())({})
    assert result["ok"] is False
    assert "git not available" in result["error"]


# ----- 受控接口:fetch 默认不联网 / 远程失败降级 -----

@pytest.mark.asyncio
async def test_git_fetch_disabled_when_network_off(tmp_path: Path) -> None:
    result = await make_git_fetch(tmp_path, GitSettings(allow_network=False))({})
    assert result["ok"] is False
    assert "network disabled" in result["error"]


@pytest.mark.asyncio
async def test_git_fetch_degrades_on_remote_failure(tmp_path: Path, monkeypatch) -> None:
    class _FailProc:
        returncode = 128

        async def communicate(self):
            return b"", b"fatal: Authentication failed"

    async def _fake_exec(*args, **kwargs):
        return _FailProc()

    monkeypatch.setattr(git_tools.asyncio, "create_subprocess_exec", _fake_exec)
    result = await make_git_fetch(tmp_path, GitSettings(allow_network=True))({"remote": "origin"})
    # 远程权限不足 → 降级,不抛穿。
    assert result["ok"] is False
    assert "git_fetch failed" in result["error"]


# ----- 受控接口:checkout 默认拒绝(不隐式切分支)-----

@pytest.mark.asyncio
async def test_git_checkout_refuses_by_default(tmp_path: Path) -> None:
    result = await make_git_checkout(tmp_path, GitSettings())({"ref": "feature"})
    assert result["ok"] is False
    assert "refused" in result["error"]


# ----- 真实只读 git(需要 git) -----

@_requires_git
@pytest.mark.asyncio
async def test_git_status_and_rev_parse_on_real_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    (repo / "f.txt").write_text("hi\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init"],
        cwd=repo,
        check=True,
    )

    status = await make_git_status(repo, GitSettings())({})
    assert status["ok"] is True
    assert status["data"]["stdout"] == ""  # 干净工作区

    head = await make_git_rev_parse(repo, GitSettings())({"ref": "HEAD"})
    assert head["ok"] is True
    assert len(head["data"]["stdout"].strip()) == 40  # SHA-1


@_requires_git
@pytest.mark.asyncio
async def test_git_status_degrades_on_non_repo(tmp_path: Path) -> None:
    result = await make_git_status(tmp_path, GitSettings())({})
    assert result["ok"] is False
    assert "git_status failed" in result["error"]
