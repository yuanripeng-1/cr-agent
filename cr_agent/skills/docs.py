"""
加载每个 skill 目录下的 SKILL.md 作为该 skill 暴露给主 agent 的工具描述。
"""

from __future__ import annotations

from pathlib import Path


_SKILLS_ROOT = Path(__file__).resolve().parent


def load_skill_doc(skill_name: str) -> str:
    path = _SKILLS_ROOT / skill_name / "SKILL.md"
    return path.read_text(encoding="utf-8").strip()


def load_skill_description(skill_name: str) -> str:
    return load_skill_doc(skill_name)
