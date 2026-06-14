from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cr_agent.core.agent_config import CrgConfig
from cr_agent.core.review_input import ReviewInput
from cr_agent.tools.cr_native import crg_lifecycle
from cr_agent.tools.cr_native.crg_lifecycle import build_crg_lifecycle


class _FakeProc:
    def __init__(self, returncode: int = 0, stdout: bytes = b"ok\n", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr
        self.killed = False

    async def communicate(self):
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True

    async def wait(self):
        return self.returncode


class _SlowProc(_FakeProc):
    async def communicate(self):
        await asyncio.sleep(1)
        return b"", b""


@pytest.fixture
def roots(tmp_path: Path):
    workspace = tmp_path / "workspace" / "task-1"
    project_root = workspace / "source"
    target_root = tmp_path / "target"
    result_dir = workspace / "cr_result"
    config_dir = tmp_path
    project_root.mkdir(parents=True)
    target_root.mkdir()
    result_dir.mkdir()
    return {
        "workspace": workspace,
        "project_root": project_root,
        "target_root": target_root,
        "result_dir": result_dir,
        "config_dir": config_dir,
    }


def _review_input(project_root: Path, *, target_branch: str = "release/x") -> ReviewInput:
    return ReviewInput.model_validate(
        {
            "task_id": "task-1",
            "title": "fix",
            "diff_content": "diff --git a/a.py b/a.py",
            "project_root": str(project_root),
            "project_id": 35,
            "source_branch": "feature/a",
            "target_branch": target_branch,
        }
    )


def _lifecycle(roots, *, enabled: bool = True, target_branch: str = "release/x"):
    return build_crg_lifecycle(
        config=CrgConfig(
            enabled=enabled,
            base_dir=str(roots["config_dir"] / ".crg"),
            target_root=str(roots["target_root"]),
            max_retry=2,
            retry_interval_s=0.01,
            timeout_s=0.05,
        ),
        review_input=_review_input(roots["project_root"], target_branch=target_branch),
        workspace_dir=roots["workspace"],
        result_dir=roots["result_dir"],
        config_dir=roots["config_dir"],
    )


def _patch_crg(monkeypatch, calls: list[tuple[str, ...]], proc_factory=None) -> None:
    monkeypatch.setattr(crg_lifecycle, "resolve_command", lambda candidates: "crg")
    monkeypatch.setattr(crg_lifecycle, "subprocess_run_cp_reflink", lambda src, dst: 1)

    async def _fake_exec(*args, **kwargs):
        calls.append(tuple(args))
        if proc_factory is not None:
            return proc_factory(*args)
        return _FakeProc()

    monkeypatch.setattr(crg_lifecycle.asyncio, "create_subprocess_exec", _fake_exec)


def test_crg_disabled_does_not_start(roots) -> None:
    life = _lifecycle(roots, enabled=False)
    life.start_background()
    assert life.task is None
    assert life.ready is False
    assert "CRG_DISABLED reason=enabled_false" in (roots["result_dir"] / "run.log").read_text()


@pytest.mark.asyncio
async def test_crg_enabled_target_db_update_mr_graph(roots, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    _patch_crg(monkeypatch, calls)
    life = _lifecycle(roots)
    assert life.paths is not None
    life.paths.baseline_dir.mkdir(parents=True)
    life.paths.target_db.write_text("target-db", encoding="utf-8")

    life.start_background()
    await life.task

    assert life.ready is True
    assert life.paths.graph_db.read_text(encoding="utf-8") == "target-db"
    assert calls == [("crg", "update", str(roots["project_root"]), "--db", str(life.paths.graph_db))]


@pytest.mark.asyncio
async def test_crg_enabled_main_db_two_phase_update(roots, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    _patch_crg(monkeypatch, calls)
    life = _lifecycle(roots)
    assert life.paths is not None
    life.paths.baseline_dir.mkdir(parents=True)
    (life.paths.baseline_dir / "main.db").write_text("main-db", encoding="utf-8")

    life.start_background()
    await life.task

    assert life.ready is True
    assert life.paths.target_db.read_text(encoding="utf-8") == "main-db"
    assert calls == [
        ("crg", "update", str(roots["target_root"]), "--db", str(life.paths.target_db)),
        ("crg", "update", str(roots["project_root"]), "--db", str(life.paths.graph_db)),
    ]


@pytest.mark.asyncio
async def test_crg_enabled_cold_build_then_update(roots, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def _proc_factory(*args):
        if args[1] == "build":
            life.paths.target_db.write_text("built-db", encoding="utf-8")
        return _FakeProc()

    _patch_crg(monkeypatch, calls, _proc_factory)
    life = _lifecycle(roots)
    assert life.paths is not None

    life.start_background()
    await life.task

    assert life.ready is True
    assert calls == [
        ("crg", "build", str(roots["target_root"]), "--db", str(life.paths.target_db)),
        ("crg", "update", str(roots["project_root"]), "--db", str(life.paths.graph_db)),
    ]


@pytest.mark.asyncio
async def test_crg_target_branch_main_uses_target_root_only(roots, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def _proc_factory(*args):
        if args[1] == "build":
            life.paths.target_db.write_text("main-db", encoding="utf-8")
        return _FakeProc()

    _patch_crg(monkeypatch, calls, _proc_factory)
    life = _lifecycle(roots, target_branch="main")
    assert life.paths is not None

    life.start_background()
    await life.task

    assert life.paths.target_db.name == "main.db"
    assert calls[0] == ("crg", "build", str(roots["target_root"]), "--db", str(life.paths.target_db))


@pytest.mark.asyncio
async def test_crg_build_timeout_degrades(roots, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    _patch_crg(monkeypatch, calls, lambda *args: _SlowProc())
    life = _lifecycle(roots)

    life.start_background()
    await life.task

    assert life.ready is False
    assert life.warnings
    assert "timed out" in life.warnings[0]


@pytest.mark.asyncio
async def test_crg_query_skips_after_max_retry(roots, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    _patch_crg(monkeypatch, calls)
    life = _lifecycle(roots)

    result = await life.query("PaymentService.acquireLock")

    assert result["ok"] is False
    assert "not ready" in result["error"]
    assert calls == []


@pytest.mark.asyncio
async def test_crg_query_retries_until_ready(roots, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    _patch_crg(monkeypatch, calls)
    life = _lifecycle(roots)
    assert life.paths is not None
    life.ready = True
    life.paths.graph_db.parent.mkdir(parents=True, exist_ok=True)
    life.paths.graph_db.write_text("graph", encoding="utf-8")

    result = await life.query("PaymentService.acquireLock")

    assert result["ok"] is True
    assert result["data"]["query"] == "PaymentService.acquireLock"
    assert calls == [
        ("crg", "query", "PaymentService.acquireLock", "--db", str(life.paths.graph_db))
    ]


@pytest.mark.asyncio
async def test_crg_never_calls_git_checkout(roots, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    _patch_crg(monkeypatch, calls)
    life = _lifecycle(roots)
    assert life.paths is not None
    life.paths.baseline_dir.mkdir(parents=True)
    life.paths.target_db.write_text("target-db", encoding="utf-8")

    life.start_background()
    await life.task

    flat = " ".join(" ".join(call) for call in calls)
    assert "checkout" not in flat
