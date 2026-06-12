from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable, TypeVar

from cr_agent.core.agent_config import load_agent_config
from cr_agent.core.review_input import load_review_input
from cr_agent.core.review_output import load_review_result


T = TypeVar("T")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate frozen CR-Agent contracts.")
    parser.add_argument("--config", required=True, help="Path to agent_config.toml")
    parser.add_argument("--context", required=True, help="Path to context.json")
    parser.add_argument("--result", required=True, help="Path to result.json")
    return parser


def _load_contract(label: str, path: Path, loader: Callable[[Path], T]) -> T:
    try:
        return loader(path)
    except Exception as exc:
        raise ValueError(f"{label} contract failed: {path}\n{exc}") from exc


def main() -> int:
    args = _build_parser().parse_args()

    config_path = Path(args.config).expanduser().resolve()
    context_path = Path(args.context).expanduser().resolve()
    result_path = Path(args.result).expanduser().resolve()

    # schemas/ 目录用于保存“原格式字段说明”契约样例;运行时强校验由
    # core/ 下的 Pydantic 模型完成,避免文档样例和机器 schema 混在一起。
    try:
        agent_config = _load_contract("agent_config", config_path, load_agent_config)
        review_input = _load_contract("context", context_path, load_review_input)
        review_result = _load_contract("result", result_path, load_review_result)
    except ValueError as exc:
        print(f"[contracts] failed: {exc}")
        return 1

    print("[contracts] agent_config ok")
    print(f"[contracts] context ok task_id={review_input.task_id}")
    print(
        "[contracts] result ok "
        f"status={review_result.status} issues={len(review_result.issues)} "
        f"comments={len(review_result.line_comments.comments)}"
    )
    if agent_config.configured_platform():
        print(f"[contracts] platform={agent_config.configured_platform()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
