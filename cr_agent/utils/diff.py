from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class DiffFile:
    old_path: str
    new_path: str
    added_lines: set[int] = field(default_factory=set)


def parse_changed_files(diff_content: str) -> list[DiffFile]:
    files: list[DiffFile] = []
    current_old = ""
    current_new = ""
    added_lines: set[int] = set()
    new_line: int | None = None

    def flush() -> None:
        nonlocal current_old, current_new, added_lines, new_line
        if current_new:
            files.append(
                DiffFile(
                    old_path=_strip_prefix(current_old, "a/"),
                    new_path=_strip_prefix(current_new, "b/"),
                    added_lines=set(added_lines),
                )
            )
        current_old = ""
        current_new = ""
        added_lines = set()
        new_line = None

    for line in diff_content.splitlines():
        if line.startswith("diff --git "):
            flush()
            parts = line.split()
            if len(parts) >= 4:
                current_old = parts[2]
                current_new = parts[3]
            continue
        if line.startswith("--- "):
            current_old = line[4:].strip()
            continue
        if line.startswith("+++ "):
            current_new = line[4:].strip()
            continue
        if line.startswith("@@ "):
            new_line = _parse_new_hunk_start(line)
            continue
        if new_line is None:
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added_lines.add(new_line)
            new_line += 1
        elif line.startswith("-") and not line.startswith("---"):
            continue
        else:
            new_line += 1
    flush()
    return files


def changed_file_payload(diff_content: str) -> list[dict[str, object]]:
    return [
        {
            "old_path": item.old_path,
            "new_path": item.new_path,
            "added_lines": sorted(item.added_lines),
        }
        for item in parse_changed_files(diff_content)
    ]


def added_line_map(diff_content: str) -> dict[str, set[int]]:
    return {item.new_path: item.added_lines for item in parse_changed_files(diff_content)}


def _parse_new_hunk_start(line: str) -> int | None:
    marker = "+"
    try:
        plus = line.index(marker)
        token = line[plus + 1 :].split()[0]
        start = token.split(",", 1)[0]
        return int(start)
    except (ValueError, IndexError):
        return None


def _strip_prefix(path: str, prefix: str) -> str:
    value = path.strip()
    if value == "/dev/null":
        return value
    return value[len(prefix) :] if value.startswith(prefix) else value
