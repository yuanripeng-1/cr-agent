from __future__ import annotations

from pathlib import Path


_SKILLS_ROOT = Path(__file__).resolve().parent


def load_skill_doc(skill_name: str) -> str:
    path = _SKILLS_ROOT / skill_name / "SKILL.md"
    return path.read_text(encoding="utf-8").strip()


def load_skill_description(skill_name: str) -> str:
    return load_skill_doc(skill_name)
