from __future__ import annotations

from pathlib import Path

import pytest

from cr_agent.tools.cr_native import external_tools
from cr_agent.tools.cr_native.external_tools import (
    check_external_tool_availability,
    make_ast_grep_search,
    make_crg_query,
    make_crg_status,
    make_semble_search,
    resolve_command,
)
from cr_agent.tools.cr_native.fs_tools import ToolLimits


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text("def handler():\n    return 1\n", encoding="utf-8")
    return root


def test_resolve_command_uses_first_available(monkeypatch) -> None:
    monkeypatch.setattr(
        external_tools.shutil,
        "which",
        lambda command: f"/bin/{command}" if command == "sg" else None,
    )
    assert resolve_command(("ast-grep", "sg")) == "sg"


def test_check_external_tool_availability_reports_missing(monkeypatch) -> None:
    monkeypatch.setattr(external_tools, "resolve_command", lambda candidates: None)
    status = check_external_tool_availability()
    assert status["git"]["available"] is False
    assert status["git"]["required"] is True
    assert status["code-review-graph"]["candidates"] == ["code-review-graph", "crg"]


@pytest.mark.asyncio
async def test_ast_grep_search_degrades_when_missing(project: Path, monkeypatch) -> None:
    monkeypatch.setattr(external_tools, "resolve_command", lambda candidates: None)
    result = await make_ast_grep_search(project, ToolLimits())({"pattern": "def $F()"})
    assert result["ok"] is False
    assert result["data"] is None
    assert "ast_grep_search" in result["error"]
    assert result["warnings"]


@pytest.mark.asyncio
async def test_semble_search_degrades_when_missing(project: Path, monkeypatch) -> None:
    monkeypatch.setattr(external_tools, "resolve_command", lambda candidates: None)
    result = await make_semble_search(project, ToolLimits())({"query": "auth flow"})
    assert result["ok"] is False
    assert result["data"] is None
    assert "semble_search" in result["error"]
    assert result["warnings"]


@pytest.mark.asyncio
async def test_crg_status_degrades_when_missing(project: Path, monkeypatch) -> None:
    monkeypatch.setattr(external_tools, "resolve_command", lambda candidates: None)
    result = await make_crg_status(project, ToolLimits())({})
    assert result["ok"] is False
    assert result["data"] is None
    assert "crg_status" in result["error"]
    assert result["warnings"]


@pytest.mark.asyncio
async def test_crg_query_degrades_when_missing(project: Path, monkeypatch) -> None:
    monkeypatch.setattr(external_tools, "resolve_command", lambda candidates: None)
    result = await make_crg_query(project, ToolLimits())({"query": "find impact"})
    assert result["ok"] is False
    assert result["data"] is None
    assert "crg_query" in result["error"]
    assert result["warnings"]


class _FakeProc:
    returncode = 0

    async def communicate(self):
        return b"app.py:1:def handler\n", b""

    def kill(self) -> None:  # pragma: no cover
        pass

    async def wait(self):  # pragma: no cover
        return 0


@pytest.mark.asyncio
async def test_ast_grep_search_uses_adapter_command(project: Path, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(external_tools, "resolve_command", lambda candidates: "sg")

    async def _fake_exec(*args, **kwargs):
        calls.append(tuple(args))
        return _FakeProc()

    monkeypatch.setattr(external_tools.asyncio, "create_subprocess_exec", _fake_exec)
    result = await make_ast_grep_search(project, ToolLimits())(
        {"pattern": "def $F()", "lang": "python", "path": "app.py"}
    )
    assert result["ok"] is True
    assert result["data"]["matches"] == ["app.py:1:def handler"]
    assert calls[0][:4] == ("sg", "run", "--pattern", "def $F()")
