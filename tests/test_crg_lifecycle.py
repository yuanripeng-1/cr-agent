from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cr_agent.core.agent_config import CrgConfig
from cr_agent.core.review_input import ReviewInput
from cr_agent.tools.cr_native import crg_lifecycle
from cr_agent.tools.cr_native.crg_lifecycle import build_crg_lifecycle, plan_crg_paths


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


def _review_input(
    project_root: Path,
    *,
    target_branch: str = "release/x",
    task_id: str = "task-1",
    project_id: int | None = 35,
    mr_iid: int | None = 7,
    source_branch: str = "feature/a",
) -> ReviewInput:
    return ReviewInput.model_validate(
        {
            "task_id": task_id,
            "title": "fix",
            "diff_content": "diff --git a/a.py b/a.py",
            "project_root": str(project_root),
            "project_id": project_id,
            "mr_iid": mr_iid,
            "base_sha": "abc123",
            "source_branch": source_branch,
            "target_branch": target_branch,
        }
    )


def _lifecycle(roots, *, enabled: bool = True, target_branch: str = "release/x"):
    return build_crg_lifecycle(
        config=CrgConfig(
            enabled=enabled,
            base_dir=".crg",
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
    monkeypatch.setattr(crg_lifecycle, "resolve_command", lambda candidates: "code-review-graph")

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


def test_crg_paths_use_shared_workspace_root_and_mr_lineage(roots) -> None:
    review_input = _review_input(
        roots["project_root"],
        task_id="task-a",
        project_id=35,
        mr_iid=7,
        source_branch="feature/a",
    )

    paths = plan_crg_paths(
        CrgConfig(base_dir=".crg"),
        review_input,
        roots["workspace"],
        roots["config_dir"],
    )

    assert paths.data_dir == roots["workspace"].parent / ".crg" / "projects" / "35" / "mrs" / "7"
    assert "task-a" not in str(paths.data_dir)


def test_crg_paths_fall_back_to_source_branch_without_mr_iid(roots) -> None:
    review_input = _review_input(
        roots["project_root"],
        task_id="task-a",
        project_id=35,
        mr_iid=None,
        source_branch="feature/a",
    )

    paths = plan_crg_paths(
        CrgConfig(base_dir=".crg"),
        review_input,
        roots["workspace"],
        roots["config_dir"],
    )

    assert paths.data_dir == (
        roots["workspace"].parent / ".crg" / "projects" / "35" / "branches" / "feature__a"
    )


def test_crg_paths_fall_back_to_project_root_and_default_lineage(roots) -> None:
    review_input = _review_input(
        roots["project_root"],
        task_id="task-a",
        project_id=None,
        mr_iid=None,
        source_branch="",
    )

    paths = plan_crg_paths(
        CrgConfig(base_dir=".crg"),
        review_input,
        roots["workspace"],
        roots["config_dir"],
    )

    assert paths.data_dir == (
        roots["workspace"].parent / ".crg" / "projects" / "source" / "default"
    )


@pytest.mark.asyncio
async def test_crg_enabled_existing_graph_updates_source_repo(roots, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    # 已构建的图:status 必须报告 Nodes>0,才走增量 update;
    # 否则(空图/Nodes:0/never)应冷启动 build。
    def _proc_factory(*args):
        if args[1] == "status":
            return _FakeProc(stdout=b"Nodes: 42\nEdges: 99\nFiles: 7\nLast updated: 2026-01-01\n")
        return _FakeProc()

    _patch_crg(monkeypatch, calls, _proc_factory)
    life = _lifecycle(roots)
    assert life.paths is not None

    life.start_background()
    await life.task

    assert life.ready is True
    assert calls == [
        (
            "code-review-graph",
            "status",
            "--repo",
            str(roots["project_root"]),
            "--data-dir",
            str(life.paths.data_dir),
        ),
        (
            "code-review-graph",
            "update",
            "--repo",
            str(roots["project_root"]),
            "--base",
            "abc123",
            "--data-dir",
            str(life.paths.data_dir),
        ),
    ]


@pytest.mark.asyncio
async def test_crg_empty_graph_status_ok_triggers_build(roots, monkeypatch) -> None:
    # status 退出码 0 但图为空(Nodes:0 / Last updated:never)时,必须冷启动 build,
    # 不能走增量 update(否则空图永远不会被填充)。
    calls: list[tuple[str, ...]] = []

    def _proc_factory(*args):
        if args[1] == "status":
            return _FakeProc(stdout=b"Nodes: 0\nEdges: 0\nFiles: 0\nLast updated: never\n")
        return _FakeProc()

    _patch_crg(monkeypatch, calls, _proc_factory)
    life = _lifecycle(roots)

    life.start_background()
    await life.task

    assert life.ready is True
    subcommands = [call[1] for call in calls]
    assert subcommands == ["status", "build"]


@pytest.mark.asyncio
async def test_crg_enabled_cold_build(roots, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    def _proc_factory(*args):
        if args[1] == "status":
            return _FakeProc(returncode=1, stderr=b"not initialized")
        return _FakeProc()

    _patch_crg(monkeypatch, calls, _proc_factory)
    life = _lifecycle(roots)
    assert life.paths is not None

    life.start_background()
    await life.task

    assert life.ready is True
    assert calls == [
        (
            "code-review-graph",
            "status",
            "--repo",
            str(roots["project_root"]),
            "--data-dir",
            str(life.paths.data_dir),
        ),
        (
            "code-review-graph",
            "build",
            "--repo",
            str(roots["project_root"]),
            "--data-dir",
            str(life.paths.data_dir),
        ),
    ]


@pytest.mark.asyncio
async def test_crg_uses_source_project_root_not_target_root(roots, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    _patch_crg(monkeypatch, calls)
    life = _lifecycle(roots, target_branch="main")
    assert life.paths is not None

    life.start_background()
    await life.task

    flat = " ".join(" ".join(call) for call in calls)
    assert str(roots["project_root"]) in flat
    assert str(roots["target_root"]) not in flat


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

    result = await life.query("PaymentService.acquireLock")

    assert result["ok"] is True
    assert result["data"]["query"] == "PaymentService.acquireLock"
    assert result["data"]["base"] == "abc123"
    assert calls == [
        (
            "code-review-graph",
            "detect-changes",
            "--repo",
            str(roots["project_root"]),
            "--base",
            "abc123",
        )
    ]


@pytest.mark.asyncio
async def test_crg_never_calls_git_checkout(roots, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    _patch_crg(monkeypatch, calls)
    life = _lifecycle(roots)
    assert life.paths is not None

    life.start_background()
    await life.task

    flat = " ".join(" ".join(call) for call in calls)
    assert "checkout" not in flat


@pytest.mark.asyncio
async def test_build_or_update_disabled_returns_error(roots) -> None:
    life = _lifecycle(roots, enabled=False)
    result = await life.build_or_update()
    assert result["ok"] is False
    assert "disabled" in result["error"]


@pytest.mark.asyncio
async def test_build_or_update_when_ready_is_idempotent(roots, monkeypatch) -> None:
    _patch_crg(monkeypatch, [])
    life = _lifecycle(roots)
    life.ready = True

    result = await life.build_or_update()

    assert result["ok"] is True
    assert result["data"]["already_ready"] is True


@pytest.mark.asyncio
async def test_build_or_update_waits_for_background_task(roots, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []
    _patch_crg(monkeypatch, calls)
    life = _lifecycle(roots)
    life.start_background()

    result = await life.build_or_update()

    assert result["ok"] is True
    assert life.ready is True
    assert any(call[1] == "build" for call in calls)


@pytest.mark.asyncio
async def test_build_or_update_starts_build_when_not_started(roots, monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def _proc_factory(*args):
        if args[1] == "status":
            return _FakeProc(stdout=b"Nodes: 0\nEdges: 0\nFiles: 0\nLast updated: never\n")
        return _FakeProc()

    _patch_crg(monkeypatch, calls, _proc_factory)
    life = _lifecycle(roots)

    result = await life.build_or_update()

    assert result["ok"] is True
    assert life.ready is True
    assert [call[1] for call in calls] == ["status", "build"]
