from __future__ import annotations

import asyncio
from typing import Any

from cr_agent.core.errors import (
    RuntimeCallError,
    RuntimeContextLimitError,
    RuntimeTimeoutError,
    RuntimeTokenLimitError,
)
from cr_agent.core.types import QueryResult
from cr_agent.core.usage import extract_usage


class ClaudeAgentRuntime:
    """
    Thin adapter around Claude Agent SDK calls.

    Tests inject a fake client so this module can be exercised without calling
    Claude Agent SDK, LiteLLM, or any external model service.
    """

    def __init__(self, config: Any, client: Any | None = None) -> None:
        self.config = config
        self._client = client

    async def query_main(
        self,
        prompt: str,
        *,
        assembled_options: Any | None = None,
        timeout_s: float = 300,
    ) -> QueryResult:
        return await self._query(
            agent_name="main",
            prompt=prompt,
            assembled_options=assembled_options,
            timeout_s=timeout_s,
        )

    async def query_subagent(
        self,
        agent_name: str,
        prompt: str,
        *,
        assembled_options: Any | None = None,
        timeout_s: float = 300,
    ) -> QueryResult:
        return await self._query(
            agent_name=agent_name,
            prompt=prompt,
            assembled_options=assembled_options,
            timeout_s=timeout_s,
        )

    async def _query(
        self,
        *,
        agent_name: str,
        prompt: str,
        assembled_options: Any | None,
        timeout_s: float,
    ) -> QueryResult:
        if self._client is None:
            raise RuntimeCallError("Claude Agent SDK client is not configured")

        try:
            raw_response = await asyncio.wait_for(
                self._invoke_client(
                    agent_name=agent_name,
                    prompt=prompt,
                    assembled_options=assembled_options,
                ),
                timeout=timeout_s,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeTimeoutError(
                f"Runtime call timed out for agent={agent_name}"
            ) from exc
        except RuntimeCallError:
            raise
        except Exception as exc:
            raise _classify_runtime_error(exc, agent_name) from exc

        text = _extract_text(raw_response)
        if not text.strip():
            raise RuntimeCallError(f"Runtime returned empty result for agent={agent_name}")

        return QueryResult(
            text=text,
            usage=extract_usage(raw_response),
            raw=raw_response,
        )

    async def _invoke_client(
        self,
        *,
        agent_name: str,
        prompt: str,
        assembled_options: Any | None,
    ) -> Any:
        query = getattr(self._client, "query", None)
        if query is None:
            raise RuntimeCallError("Injected SDK client does not expose query()")
        return await query(
            agent_name=agent_name,
            prompt=prompt,
            assembled_options=assembled_options,
        )


def _extract_text(raw_response: Any) -> str:
    if isinstance(raw_response, str):
        return raw_response
    if isinstance(raw_response, dict):
        for key in ("text", "content", "result"):
            value = raw_response.get(key)
            if isinstance(value, str):
                return value
        return ""
    for attr in ("text", "content", "result"):
        value = getattr(raw_response, attr, None)
        if isinstance(value, str):
            return value
    return ""


def _classify_runtime_error(exc: Exception, agent_name: str) -> RuntimeCallError:
    message = str(exc)
    lowered = message.lower()
    if "token" in lowered and "limit" in lowered:
        return RuntimeTokenLimitError(
            f"Runtime token limit for agent={agent_name}: {message}"
        )
    if "context" in lowered and ("limit" in lowered or "window" in lowered):
        return RuntimeContextLimitError(
            f"Runtime context limit for agent={agent_name}: {message}"
        )
    return RuntimeCallError(f"Runtime call failed for agent={agent_name}: {message}")

