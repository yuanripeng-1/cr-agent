from __future__ import annotations

import asyncio
from typing import Any, Callable

from cr_agent.core.errors import (
    RuntimeCallError,
    RuntimeContextLimitError,
    RuntimeTimeoutError,
    RuntimeTokenLimitError,
)
from cr_agent.core.types import QueryResult
from cr_agent.core.usage import extract_usage
from cr_agent.utils.logging import get_logger

_logger = get_logger("cr_agent.core.sdk_runtime")


def build_sdk_env(llm: Any) -> dict[str, str]:
    """
    把 [llm].api_base/api_key 映射成 SDK/CLI 识别的环境变量。

    - api_base -> ANTHROPIC_BASE_URL(指向 LiteLLM Anthropic-compatible Gateway)
    - api_key  -> ANTHROPIC_API_KEY(x-api-key 鉴权)

    空值不写入,避免用空字符串覆盖宿主已有的环境配置。bootstrap 与 SdkQueryClient
    共用这一份映射,保证“早期 env”与“调用时 options.env”一致。
    """
    env: dict[str, str] = {}
    api_base = getattr(llm, "api_base", "") or ""
    api_key = getattr(llm, "api_key", "") or ""
    if api_base:
        env["ANTHROPIC_BASE_URL"] = api_base
    if api_key:
        env["ANTHROPIC_API_KEY"] = api_key
    return env


class SdkQueryClient:
    """
    真实 Claude Agent SDK client 适配器,统一走 LiteLLM Anthropic-compatible Gateway。

    实现 ClaudeAgentRuntime 期望的注入 client 契约:
        async query(*, agent_name, prompt, assembled_options) -> dict

    query_fn 默认是 claude_agent_sdk.query(异步生成器),测试可注入 fake 生成器,
    从而在不启动 claude CLI / 不联网的情况下验证消息解析与 usage 提取。
    """

    def __init__(
        self,
        *,
        model: str,
        env: dict[str, str] | None = None,
        query_fn: Callable[..., Any] | None = None,
    ) -> None:
        self.model = model
        self.env = env or {}
        self._query_fn = query_fn

    def _resolve_query_fn(self) -> Callable[..., Any]:
        if self._query_fn is not None:
            return self._query_fn
        # 惰性导入:仅在真正发起调用时才依赖 SDK(其底层会拉起 claude CLI)。
        from claude_agent_sdk import query as sdk_query

        return sdk_query

    def _build_options(self) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        # tools=[] 禁用内建工具:PR2 只做最小模型冒烟,不接任何真实工具。
        return ClaudeAgentOptions(model=self.model, env=self.env, tools=[])

    async def query(
        self,
        *,
        agent_name: str,
        prompt: str,
        assembled_options: Any | None = None,
    ) -> dict[str, Any]:
        query_fn = self._resolve_query_fn()
        options = assembled_options if assembled_options is not None else self._build_options()

        texts: list[str] = []
        final_text: str | None = None
        usage: dict[str, Any] | None = None
        is_error = False
        error_detail = ""

        async for message in query_fn(prompt=prompt, options=options):
            content = getattr(message, "content", None)
            if content is not None:
                for block in content:
                    block_text = getattr(block, "text", None)
                    if isinstance(block_text, str):
                        texts.append(block_text)
            # ResultMessage 携带最终文本与 usage。
            if hasattr(message, "usage") and getattr(message, "usage") is not None:
                usage = getattr(message, "usage")
            if hasattr(message, "is_error"):
                is_error = bool(getattr(message, "is_error"))
                result_text = getattr(message, "result", None)
                if isinstance(result_text, str):
                    final_text = result_text
                errors = getattr(message, "errors", None)
                if errors:
                    error_detail = "; ".join(str(e) for e in errors)

        if is_error:
            error = RuntimeCallError(
                f"Model reported error for agent={agent_name}: {error_detail or 'unknown'}"
            )
            # 把已计费 usage 带回上游,避免失败时丢 token / 写假 0。
            error.usage = usage  # type: ignore[attr-defined]
            raise error

        text = final_text if final_text else "".join(texts)
        return {"text": text, "usage": usage}


def build_runtime(config: Any) -> "ClaudeAgentRuntime":
    """
    从 per-task agent_config.toml 的 [llm] 读取 model/api_base/api_key,
    构造走 LiteLLM 网关的真实 Runtime。
    """
    llm = config.llm
    client = SdkQueryClient(model=llm.model, env=build_sdk_env(llm))
    return ClaudeAgentRuntime(config=config, client=client)


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

        model = getattr(self._client, "model", "?")
        _logger.info("MODEL_CALL_START agent=%s model=%s", agent_name, model)
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
            _logger.error("MODEL_CALL_ERROR agent=%s reason=timeout", agent_name)
            raise RuntimeTimeoutError(
                f"Runtime call timed out for agent={agent_name}"
            ) from exc
        except RuntimeCallError as exc:
            _logger.error("MODEL_CALL_ERROR agent=%s reason=%s", agent_name, exc)
            raise
        except Exception as exc:
            _logger.error("MODEL_CALL_ERROR agent=%s reason=%s", agent_name, exc)
            raise _classify_runtime_error(exc, agent_name) from exc

        text = _extract_text(raw_response)
        if not text.strip():
            _logger.error("MODEL_CALL_ERROR agent=%s reason=empty_result", agent_name)
            raise RuntimeCallError(f"Runtime returned empty result for agent={agent_name}")

        usage = extract_usage(raw_response)
        _logger.info(
            "MODEL_CALL_END agent=%s input=%s output=%s cache_creation=%s cache_read=%s",
            agent_name,
            usage.input_tokens,
            usage.output_tokens,
            usage.cache_creation_tokens,
            usage.cache_read_tokens,
        )
        return QueryResult(
            text=text,
            usage=usage,
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

