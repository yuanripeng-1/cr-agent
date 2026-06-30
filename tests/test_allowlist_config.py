from __future__ import annotations

from cr_agent.tools.allowlist import default_agent_tools_path, load_agent_tools


def test_load_real_agent_tools_config() -> None:
    config = load_agent_tools(default_agent_tools_path())
    # config/agent_tools.toml 的声明应被原样读出。
    assert config["context"] == ["*"]
    assert config["dimension"] == ["read_file_range", "grep_text"]
    assert config["summary"] == []
    assert config["main"] == []


def test_missing_config_returns_empty(tmp_path) -> None:
    assert load_agent_tools(tmp_path / "absent.toml") == {}
