from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

import pytest

from cr_agent.tools.cr_native import fs_tools
from cr_agent.tools.cr_native.fs_tools import (
    ToolLimits,
    make_glob_files,
    make_grep_text,
    make_read_file,
    make_read_file_range,
)

_HAS_RG = shutil.which("rg") is not None
_requires_rg = pytest.mark.skipif(not _HAS_RG, reason="rg executable not on PATH")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "a.py").write_text("line1\nline2\nneedle here\nline4\n", encoding="utf-8")
    (root / "sub").mkdir()
    (root / "sub" / "b.txt").write_text("hello\n", encoding="utf-8")
    return root


# ----- 正常路径 -----

@pytest.mark.asyncio
async def test_read_file_ok(project: Path) -> None:
    result = await make_read_file(project, ToolLimits())({"path": "a.py"})
    assert result["ok"] is True
    assert "needle here" in result["data"]["content"]
    assert result["warnings"] == []


@pytest.mark.asyncio
async def test_read_file_range_ok(project: Path) -> None:
    result = await make_read_file_range(project, ToolLimits())(
        {"path": "a.py", "start_line": 2, "end_line": 3}
    )
    assert result["ok"] is True
    assert result["data"]["content"] == "line2\nneedle here"


@pytest.mark.asyncio
async def test_read_file_range_truncates_lines(project: Path) -> None:
    many = project / "many.txt"
    many.write_text("\n".join(f"line{i}" for i in range(1, 151)), encoding="utf-8")

    result = await make_read_file_range(
        project,
        ToolLimits(read_file_range_max_lines=10),
    )({"path": "many.txt", "start_line": 1, "end_line": 50})

    assert result["ok"] is True
    assert result["data"]["end_line"] == 10
    assert result["data"]["requested_end_line"] == 50
    assert len(result["data"]["content"].splitlines()) == 10
    assert any("line range truncated" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_read_file_range_truncates_content_bytes(project: Path) -> None:
    big = project / "wide.txt"
    big.write_text("A" * 5000, encoding="utf-8")

    result = await make_read_file_range(
        project,
        ToolLimits(read_file_range_max_content_bytes=100),
    )({"path": "wide.txt", "start_line": 1, "end_line": 1})

    assert result["ok"] is True
    assert len(result["data"]["content"].encode("utf-8")) == 100
    assert any("content truncated" in w for w in result["warnings"])


@pytest.mark.asyncio
async def test_glob_files_ok(project: Path) -> None:
    result = await make_glob_files(project, ToolLimits())({"pattern": "**/*.txt"})
    assert result["ok"] is True
    assert "sub/b.txt" in result["data"]["matches"]


# ----- 越权路径 -----

@pytest.mark.asyncio
async def test_read_file_rejects_dotdot_escape(project: Path, tmp_path: Path) -> None:
    (tmp_path / "secret.txt").write_text("TOPSECRET", encoding="utf-8")
    result = await make_read_file(project, ToolLimits())({"path": "../secret.txt"})
    assert result["ok"] is False
    assert "escapes project_root" in result["error"]


@pytest.mark.asyncio
async def test_read_file_rejects_symlink_escape(project: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("TOPSECRET", encoding="utf-8")
    link = project / "link.txt"
    link.symlink_to(outside)
    result = await make_read_file(project, ToolLimits())({"path": "link.txt"})
    assert result["ok"] is False
    assert "escapes project_root" in result["error"]


@pytest.mark.asyncio
async def test_glob_excludes_symlink_escape(project: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    (project / "link.txt").symlink_to(outside)
    result = await make_glob_files(project, ToolLimits())({"pattern": "*.txt"})
    assert result["ok"] is True
    assert "link.txt" not in result["data"]["matches"]


# ----- 超大文件截断 -----

@pytest.mark.asyncio
async def test_read_file_truncates_oversized(project: Path) -> None:
    big = project / "big.txt"
    big.write_text("A" * 5000, encoding="utf-8")
    result = await make_read_file(project, ToolLimits(max_file_bytes=100))({"path": "big.txt"})
    assert result["ok"] is True
    assert len(result["data"]["content"]) == 100
    assert any("truncated" in w for w in result["warnings"])


# ----- grep 正常 + rg 缺失降级 -----

@_requires_rg
@pytest.mark.asyncio
async def test_grep_text_finds_match(project: Path) -> None:
    result = await make_grep_text(project, ToolLimits())({"pattern": "needle"})
    assert result["ok"] is True
    assert any("needle here" in m for m in result["data"]["matches"])


@pytest.mark.asyncio
async def test_grep_text_degrades_when_rg_missing(project: Path, monkeypatch) -> None:
    async def _raise(*args, **kwargs):
        raise FileNotFoundError("rg not found")

    monkeypatch.setattr(fs_tools.asyncio, "create_subprocess_exec", _raise)
    result = await make_grep_text(project, ToolLimits())({"pattern": "needle"})
    # rg 缺失:降级、不抛穿。
    assert result["ok"] is False
    assert "rg" in result["error"]


class _FakeProc:
    def __init__(self, stdout: bytes) -> None:
        self._stdout = stdout

    async def communicate(self):
        return self._stdout, b""

    def kill(self) -> None:  # pragma: no cover - not hit in these tests
        pass

    async def wait(self):  # pragma: no cover
        return 0


@pytest.mark.asyncio
async def test_grep_text_parses_rg_output(project: Path, monkeypatch) -> None:
    # 不依赖系统 rg:伪造 rg 输出,验证解析路径。
    async def _fake_exec(*args, **kwargs):
        return _FakeProc(b"a.py:3:needle here\n")

    monkeypatch.setattr(fs_tools.asyncio, "create_subprocess_exec", _fake_exec)
    result = await make_grep_text(project, ToolLimits())({"pattern": "needle"})
    assert result["ok"] is True
    assert result["data"]["matches"] == ["a.py:3:needle here"]


@pytest.mark.asyncio
async def test_grep_text_truncates_large_output(project: Path, monkeypatch) -> None:
    async def _fake_exec(*args, **kwargs):
        return _FakeProc(b"x" * 5000)

    monkeypatch.setattr(fs_tools.asyncio, "create_subprocess_exec", _fake_exec)
    result = await make_grep_text(project, ToolLimits(max_grep_bytes=100))({"pattern": "x"})
    assert result["ok"] is True
    assert any("truncated" in w for w in result["warnings"])


# ----- 超时降级,主流程继续 -----

@pytest.mark.asyncio
async def test_read_file_timeout_degrades(project: Path, monkeypatch) -> None:
    import time

    def _slow(path: Path, max_bytes: int):
        time.sleep(0.5)
        return "x", False

    monkeypatch.setattr(fs_tools, "_read_text_blocking", _slow)
    result = await make_read_file(project, ToolLimits(timeout_s=0.01))({"path": "a.py"})
    assert result["ok"] is False
    assert "timed out" in result["error"]


# ----- project_root 缺失:降级不崩 -----

@pytest.mark.asyncio
async def test_read_file_without_project_root_degrades() -> None:
    result = await make_read_file(None, ToolLimits())({"path": "a.py"})
    assert result["ok"] is False
    assert "project_root not configured" in result["error"]
