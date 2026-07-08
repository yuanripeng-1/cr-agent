from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from cr_agent.core.types import TokenUsage
from cr_agent.utils.logging import get_logger

_logger = get_logger("cr_agent.core.usage")


def usage_to_dict(usage: TokenUsage) -> dict[str, Any]:
    """把 TokenUsage 序列化成产物/回传用的 dict(三个 skill 共用,避免重复)。"""
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_creation_tokens": usage.cache_creation_tokens,
        "cache_read_tokens": usage.cache_read_tokens,
        "cost": usage.cost,
    }


def usage_breakdown_entry(
    *,
    stage: str,
    usage: TokenUsage,
    skill: str | None = None,
    agent: str | None = None,
    dimension: str | None = None,
    attempt: int | None = None,
    source: str | None = None,
    status: str | None = None,
    artifact_path: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "stage": stage,
        "usage": usage_to_dict(usage),
    }
    if skill:
        entry["skill"] = skill
    if agent:
        entry["agent"] = agent
    if dimension:
        entry["dimension"] = dimension
    if attempt is not None:
        entry["attempt"] = attempt
    if source:
        entry["source"] = source
    if status:
        entry["status"] = status
    if artifact_path:
        entry["artifact_path"] = artifact_path
    return entry


def coerce_cost(value: Any) -> float:
    """把 total_cost_usd 安全转 float:None 或不可转换类型一律回退 0.0(只记 warning)。"""
    if value is None:
        return 0.0
    try:
        return max(float(value), 0.0)
    except (TypeError, ValueError):
        _logger.warning("USAGE_COST_COERCE_FAILED value=%r", value)
        return 0.0


def accumulate_usage(total: TokenUsage, call: TokenUsage) -> TokenUsage:
    return TokenUsage(
        input_tokens=total.input_tokens + call.input_tokens,
        output_tokens=total.output_tokens + call.output_tokens,
        cache_creation_tokens=(
            total.cache_creation_tokens + call.cache_creation_tokens
        ),
        cache_read_tokens=total.cache_read_tokens + call.cache_read_tokens,
        cost=total.cost + call.cost,
    )


def extract_usage(raw_response: Any) -> TokenUsage:
    usage = _usage_payload(raw_response)
    cost = _extract_cost(raw_response, usage)
    if usage is None:
        return TokenUsage(cost=cost)

    return TokenUsage(
        input_tokens=_int_field(usage, "input_tokens"),
        output_tokens=_int_field(usage, "output_tokens"),
        cache_creation_tokens=_int_field(
            usage,
            "cache_creation_input_tokens",
            "cache_creation_tokens",
        ),
        cache_read_tokens=_int_field(
            usage,
            "cache_read_input_tokens",
            "cache_read_tokens",
        ),
        cost=cost,
    )


def _usage_payload(raw_response: Any) -> Any | None:
    if raw_response is None:
        return None
    if isinstance(raw_response, Mapping):
        return raw_response.get("usage")
    return getattr(raw_response, "usage", None)


def _extract_cost(raw_response: Any, usage: Any) -> float:
    # 成本来源有二:SDK 路径把 total_cost_usd 放在 raw_response 顶层;
    # 序列化的 usage 产物把 cost 内嵌在 usage 里。两处都查,取到即用。
    for source, names in (
        (raw_response, ("total_cost_usd", "cost")),
        (usage, ("cost", "total_cost_usd")),
    ):
        value = _float_field(source, *names)
        if value is not None:
            return value
    return 0.0


def _float_field(payload: Any, *names: str) -> float | None:
    if payload is None:
        return None
    for name in names:
        if isinstance(payload, Mapping):
            value = payload.get(name)
        else:
            value = getattr(payload, name, None)
        if value is not None:
            try:
                return max(float(value), 0.0)
            except (TypeError, ValueError):
                return 0.0
    return None


def _int_field(payload: Any, *names: str) -> int:
    for name in names:
        if isinstance(payload, Mapping):
            value = payload.get(name)
        else:
            value = getattr(payload, name, None)
        if value is not None:
            try:
                return max(int(value), 0)
            except (TypeError, ValueError):
                return 0
    return 0


def accumulate_usage_from_dimension_artifacts(
    dimensions_dir: Path,
    *,
    add_usage: Any,
    add_breakdown: Any | None = None,
) -> None:
    if not dimensions_dir.is_dir():
        return
    for path in sorted(dimensions_dir.glob("*.json")):
        if path.name == "manifest.json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _logger.warning(
                "USAGE_ARTIFACT_SKIP path=%s error=%s",
                path,
                exc,
            )
            continue
        if not isinstance(payload, dict):
            continue
        # 维度产物经 json.loads 得到 dict,usage 永远是 dict(不会是 TokenUsage 实例)。
        usage = payload.get("usage")
        if isinstance(usage, dict):
            extracted = extract_usage({"usage": usage})
            if extracted == TokenUsage():
                continue
            add_usage(extracted)
            if add_breakdown is not None:
                add_breakdown(
                    usage_breakdown_entry(
                        stage="dimension_review",
                        skill="dimension_review",
                        agent="dimension",
                        dimension=str(payload.get("dimension") or path.stem),
                        status=str(payload.get("status") or ""),
                        source="dimension_artifact",
                        artifact_path=str(path),
                        usage=extracted,
                    )
                )
