#!/usr/bin/env python3
"""把 Agent 的 result.json 渲染成可读的 preview.md"""
import json
import sys
from pathlib import Path
"""
指定输出路径
python3 /Users/yuanripeng/UnDone/code-review/cr-agent-18-fix/tests/render-result-json.py \
  /Users/yuanripeng/UnDone/code-review/cr-agent-18-fix/workspaces/49-3d646d13/tmp/summary_report.json \
  /Users/yuanripeng/UnDone/code-review/cr-agent-18-fix/workspaces/49-3d646d13/tmp/review-preview.md
"""

def render_line_comments(line_comments: dict) -> str:
    comments = (line_comments or {}).get("comments") or []
    if not comments:
        return "_（无行级评论）_\n"

    parts = ["## 行级评论（Line Comments）\n"]
    for i, c in enumerate(comments, 1):
        path = c.get("new_path") or c.get("old_path") or "(unknown)"
        start = c.get("start_line", "?")
        end = c.get("end_line", "?")
        body = (c.get("body") or "").strip()
        parts.append(f"### {i}. `{path}` L{start}–L{end}\n\n{body}\n")
    return "\n".join(parts)


def main():
    if len(sys.argv) < 2:
        print("用法: render-result-json.py <result.json> [output.md]", file=sys.stderr)
        sys.exit(1)

    src = Path(sys.argv[1])
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else src.with_name("preview.md")

    data = json.loads(src.read_text(encoding="utf-8"))

    status = data.get("status", "")
    log_path = data.get("log_path", "")
    tokens = data.get("tokens_consume") or {}

    lines = [
        "# Agent 审查结果预览\n",
        f"- **status**: `{status}`",
        f"- **log_path**: `{log_path}`",
    ]
    if tokens:
        lines.append(
            f"- **tokens**: input={tokens.get('input_tokens', 0)}, "
            f"output={tokens.get('output_tokens', 0)}, cost={tokens.get('cost', 0)}"
        )
    lines.append("\n---\n\n")
    llm = data.get("llm_result")
    lines.append("## 总体报告（llm_result）\n\n")
    lines.append(llm if llm else "_（空）_\n")
    lines.append("\n---\n\n")
    lines.append(render_line_comments(data.get("line_comments")))

    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"已写入: {out}")


if __name__ == "__main__":
    main()
