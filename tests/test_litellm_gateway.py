from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from cr_agent.core import litellm_gateway as gw_mod
from cr_agent.core.litellm_gateway import (
    _MASTER_KEY_ENV,
    _UPSTREAM_KEY_ENV,
    LiteLLMGateway,
)


def _make_gateway(**overrides) -> LiteLLMGateway:
    params = dict(
        model="glm-4-flash",
        upstream_api_base="https://open.bigmodel.cn/api/paas/v4",
        upstream_api_key="UPSTREAM-SECRET-KEY",
        provider="openai",
        port=12345,
        master_key="sk-master-secret",
    )
    params.update(overrides)
    return LiteLLMGateway(**params)


class _FakeProc:
    """最小化 subprocess.Popen 替身,记录生命周期调用。"""

    def __init__(self, *, poll_value=None) -> None:
        self._poll_value = poll_value
        self.returncode = poll_value if poll_value is not None else 0
        self.terminated = False
        self.killed = False
        self.waited = False

    def poll(self):
        return self._poll_value

    def terminate(self) -> None:
        self.terminated = True
        self._poll_value = -15

    def wait(self, timeout=None) -> int:
        self.waited = True
        return 0

    def kill(self) -> None:
        self.killed = True


# ----- 基础属性 -----

def test_base_url_and_api_key_properties() -> None:
    gw = _make_gateway()
    assert gw.base_url == "http://127.0.0.1:12345"
    assert gw.api_key == "sk-master-secret"


# ----- _write_config:结构正确 + 密钥不落盘 -----

def test_write_config_structure_and_no_plaintext_secret() -> None:
    gw = _make_gateway()
    try:
        config_path = gw._write_config()
        raw = config_path.read_text(encoding="utf-8")
        data = yaml.safe_load(raw)

        entry = data["model_list"][0]
        assert entry["model_name"] == "glm-4-flash"
        assert entry["litellm_params"]["model"] == "openai/glm-4-flash"
        assert entry["litellm_params"]["api_base"] == "https://open.bigmodel.cn/api/paas/v4"
        # 关键开关:走 chat/completions 而非 /responses。
        assert data["litellm_settings"]["use_chat_completions_url_for_anthropic_messages"] is True

        # 密钥只能是 os.environ 引用,绝不落盘明文。
        assert entry["litellm_params"]["api_key"] == f"os.environ/{_UPSTREAM_KEY_ENV}"
        assert data["general_settings"]["master_key"] == f"os.environ/{_MASTER_KEY_ENV}"
        assert "UPSTREAM-SECRET-KEY" not in raw
        assert "sk-master-secret" not in raw
    finally:
        gw._cleanup_files()


# ----- _build_env:剔除宿主 ANTHROPIC_*、注入密钥与 unbuffered -----

def test_build_env_strips_host_anthropic_and_injects_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://tokenhub.infplacex.com/")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "host-key")

    gw = _make_gateway()
    env = gw._build_env()

    assert "ANTHROPIC_BASE_URL" not in env
    assert "ANTHROPIC_API_KEY" not in env
    assert env[_UPSTREAM_KEY_ENV] == "UPSTREAM-SECRET-KEY"
    assert env[_MASTER_KEY_ENV] == "sk-master-secret"
    assert env["PYTHONUNBUFFERED"] == "1"
    # 不污染调用方进程环境。
    assert os.environ.get("ANTHROPIC_BASE_URL") == "https://tokenhub.infplacex.com/"


# ----- stop 幂等 -----

def test_stop_is_idempotent_without_process() -> None:
    gw = _make_gateway()
    gw.stop()
    gw.stop()  # 第二次不应抛异常


def test_stop_terminates_process_and_cleans_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    gw = _make_gateway()
    fake = _FakeProc(poll_value=None)
    gw._proc = fake
    gw._write_config()  # 产生临时目录
    cfg_dir = gw._config_dir
    assert cfg_dir is not None and Path(cfg_dir).exists()

    gw.stop()

    assert fake.terminated is True
    assert gw._proc is None
    assert gw._config_dir is None
    assert not Path(cfg_dir).exists()
    # 再次 stop 幂等。
    gw.stop()


# ----- _wait_ready:子进程早退抛错 -----

def test_wait_ready_raises_when_process_exits_early() -> None:
    gw = _make_gateway()
    gw._proc = _FakeProc(poll_value=1)  # 已退出,returncode=1
    try:
        with pytest.raises(RuntimeError, match="启动失败"):
            gw._wait_ready()
    finally:
        gw._cleanup_files()


# ----- start():拉起进程失败时清理资源并抛出(bug1 回归) -----

def test_start_cleans_up_when_popen_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    created_dirs: list[str] = []
    real_mkdtemp = gw_mod.tempfile.mkdtemp

    def _tracking_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        created_dirs.append(path)
        return path

    monkeypatch.setattr(gw_mod.tempfile, "mkdtemp", _tracking_mkdtemp)
    monkeypatch.setattr(gw_mod, "_resolve_litellm_bin", lambda: "/usr/bin/true")

    def _boom(*args, **kwargs):
        raise OSError("popen boom")

    monkeypatch.setattr(gw_mod.subprocess, "Popen", _boom)

    log_path = tmp_path / "gw.log"
    gw = _make_gateway(log_path=log_path)

    with pytest.raises(OSError, match="popen boom"):
        gw.start()

    # 进程未拉起:句柄关闭、临时目录清理、状态复位。
    assert gw._proc is None
    assert gw._log_handle is None
    assert gw._config_dir is None
    assert created_dirs, "应至少创建过一个临时配置目录"
    for path in created_dirs:
        assert not Path(path).exists(), f"临时目录未清理: {path}"


def test_start_success_registers_atexit_and_waits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gw_mod, "_resolve_litellm_bin", lambda: "/usr/bin/true")
    fake = _FakeProc(poll_value=None)
    monkeypatch.setattr(gw_mod.subprocess, "Popen", lambda *a, **k: fake)

    waited = {"called": False}
    monkeypatch.setattr(LiteLLMGateway, "_wait_ready", lambda self: waited.__setitem__("called", True))

    registered: list = []
    monkeypatch.setattr(gw_mod.atexit, "register", lambda fn: registered.append(fn))

    gw = _make_gateway()
    try:
        gw.start()
        assert gw._proc is fake
        assert waited["called"] is True
        assert gw.stop in registered
        # start 幂等:已有进程时直接返回。
        gw.start()
    finally:
        gw.stop()
