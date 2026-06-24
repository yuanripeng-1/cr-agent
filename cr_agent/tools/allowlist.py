"""
agent_tools.toml 读取与默认路径解析。

格式:
  [agents.<agent_name>]
  tools = ["read_file", "grep_text"]   # 或 ["*"]

只解析 tools 字段(allowlist);skills 等其它字段留给对应 PR。
"""

from __future__ import annotations

from pathlib import Path

import toml

# config/agent_tools.toml 相对仓库根:cr_agent/tools/allowlist.py -> parents[2] = 仓库根。
_DEFAULT_PATH = Path(__file__).resolve().parents[2] / "config" / "agent_tools.toml"


def default_agent_tools_path() -> Path:
    return _DEFAULT_PATH


def load_agent_tools(path: Path | None = None) -> dict[str, list[str]]:
    """返回 {agent_name: [tool_name, ...]};缺失文件按空 allowlist 处理。"""
    config_path = path or _DEFAULT_PATH
    if not config_path.exists():
        return {}

    data = toml.load(config_path)
    agents = data.get("agents", {})
    result: dict[str, list[str]] = {}
    for agent_name, agent_cfg in agents.items():
        tools = agent_cfg.get("tools", []) if isinstance(agent_cfg, dict) else []
        result[agent_name] = [str(tool) for tool in tools]
    return result
