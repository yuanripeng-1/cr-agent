from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from cr_agent.core.errors import (
    RuntimeCallError,
    RuntimeContextLimitError,
    RuntimeTimeoutError,
    RuntimeTokenLimitError,
)
from cr_agent.core.sdk_runtime import ClaudeAgentRuntime


@pytest.mark.asyncio
async def test_runtime_wraps_successful_client_response() -> None:
    client = SimpleNamespace(
        query=AsyncMock(
            return_value={
                "text": "review ok",
                "usage": {
                    "input_tokens": 7,
                    "output_tokens": 8,
                    "cache_creation_input_tokens": 1,
                    "cache_read_input_tokens": 2,
                },
            }
        )
    )

    result = await ClaudeAgentRuntime(config={}, client=client).query_subagent(
        "summary",
        "prompt",
    )

    assert result.text == "review ok"
    assert result.usage.input_tokens == 7
    assert result.usage.output_tokens == 8
    assert result.usage.cache_creation_tokens == 1
    assert result.usage.cache_read_tokens == 2
    client.query.assert_awaited_once()


@pytest.mark.asyncio
async def test_runtime_rejects_empty_model_result() -> None:
    client = SimpleNamespace(query=AsyncMock(return_value={"text": ""}))

    with pytest.raises(RuntimeCallError, match="empty result"):
        await ClaudeAgentRuntime(config={}, client=client).query_subagent(
            "summary",
            "prompt",
        )


@pytest.mark.asyncio
async def test_runtime_wraps_timeout() -> None:
    async def slow_query(**kwargs):
        await asyncio.sleep(0.05)
        return {"text": "late"}

    client = SimpleNamespace(query=slow_query)

    with pytest.raises(RuntimeTimeoutError):
        await ClaudeAgentRuntime(config={}, client=client).query_subagent(
            "summary",
            "prompt",
            timeout_s=0.001,
        )


@pytest.mark.asyncio
async def test_runtime_classifies_token_limit_error() -> None:
    client = SimpleNamespace(
        query=AsyncMock(side_effect=ValueError("token limit exceeded"))
    )

    with pytest.raises(RuntimeTokenLimitError):
        await ClaudeAgentRuntime(config={}, client=client).query_subagent(
            "summary",
            "prompt",
        )


@pytest.mark.asyncio
async def test_runtime_classifies_context_limit_error() -> None:
    client = SimpleNamespace(
        query=AsyncMock(side_effect=ValueError("context window exceeded"))
    )

    with pytest.raises(RuntimeContextLimitError):
        await ClaudeAgentRuntime(config={}, client=client).query_subagent(
            "summary",
            "prompt",
        )

