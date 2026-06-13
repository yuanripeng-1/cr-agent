from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cr_agent.tools.cr_native.fs_tools import ToolLimits
from cr_agent.tools.cr_native.registry import CrNativeToolProvider

_HAS_RG = shutil.which("rg") is not None


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "app.py").write_text(
        "def handler():\n    return TODO_FIX_ME\n", encoding="utf-8"
    )
    return root


async def _scripted_agent_flow(tools: dict, keyword: str) -> dict:
    """
    模拟主 agent 的最小工具调用流程:glob 列文件 -> read 第一个 .py -> grep 关键字。
    驱动的是真实 cr-native 工具 handler。返回各步结构化结果,供断言。
    """
    glob_res = await tools["glob_files"].handler({"pattern": "**/*.py"})
    first = glob_res["data"]["matches"][0]
    read_res = await tools["read_file"].handler({"path": first})
    grep_res = await tools["grep_text"].handler({"pattern": keyword})
    return {"glob": glob_res, "read": read_res, "grep": grep_res, "file": first}


@pytest.mark.asyncio
async def test_agent_minimal_flow_over_cr_native_tools(project: Path) -> None:
    provider = CrNativeToolProvider(project_root=project, limits=ToolLimits())
    tools = {spec.name: spec for spec in provider.list_tools()}

    flow = await _scripted_agent_flow(tools, keyword="TODO_FIX_ME")

    # 每一步都返回结构化 ok 结果,且内容自洽。
    assert flow["glob"]["ok"] is True
    assert flow["file"] == "app.py"
    assert flow["read"]["ok"] is True
    assert "TODO_FIX_ME" in flow["read"]["data"]["content"]
    # grep 始终返回结构化结果:有 rg 则命中,无 rg 则降级(ok=False),都不抛穿。
    assert "ok" in flow["grep"] and "warnings" in flow["grep"]
    if _HAS_RG:
        assert flow["grep"]["ok"] is True
        assert any("TODO_FIX_ME" in m for m in flow["grep"]["data"]["matches"])
    else:
        assert flow["grep"]["ok"] is False
        assert "rg" in flow["grep"]["error"]


@pytest.mark.asyncio
async def test_unimplemented_tools_still_placeholder(project: Path) -> None:
    provider = CrNativeToolProvider(project_root=project)
    tools = {spec.name: spec for spec in provider.list_tools()}
    # PR4 未做实的工具仍是占位降级,不影响已实现工具。
    res = await tools["crg_query"].handler({"query": "x"})
    assert res["ok"] is False
    assert "not implemented" in res["error"]
