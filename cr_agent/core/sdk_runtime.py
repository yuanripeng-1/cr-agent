from __future__ import annotations

import asyncio
import json
import uuid
from collections import deque
from typing import Any, Callable

from cr_agent.core.errors import (
    RuntimeCallError,
    RuntimeContextLimitError,
    RuntimeTimeoutError,
    RuntimeTokenLimitError,
)
from cr_agent.core.types import QueryResult
from cr_agent.core.usage import extract_usage
from cr_agent.utils.logging import get_logger, redact

_logger = get_logger("cr_agent.core.sdk_runtime")
_RESULT_MESSAGE_FIELDS = (
    "subtype",
    "is_error",
    "api_error_status",
    "errors",
    "stop_reason",
    "result",
    "num_turns",
    "session_id",
)


class SdkStderrCapture:
    """Collect a small redacted tail of Claude CLI stderr for diagnostics."""

    def __init__(self, max_lines: int = 40) -> None:
        self._lines: deque[str] = deque(maxlen=max_lines)

    def __call__(self, line: str) -> None:
        clean = redact(line.rstrip())
        if clean:
            self._lines.append(clean)
            # INFO 级,使开启 --debug 后 CLI 真实报错(402/429/网络等)能落进 run.log,
            # 不再被上层 timeout 掩盖。
            _logger.info("CLAUDE_CLI_STDERR %s", clean)

    def tail(self) -> str:
        return "\n".join(self._lines)


def append_stderr_diagnostic(message: str, stderr_tail: str) -> str:
    if not stderr_tail:
        return message
    return f"{message}\nClaude CLI stderr tail:\n{stderr_tail}"


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
    # 可选:隔离 claude CLI 配置目录,避免宿主 ~/.claude/settings.json 的 env 块
    # (如 ANTHROPIC_BASE_URL/AUTH_TOKEN)覆盖本进程注入的网关地址与鉴权。
    # 用 getattr 兜底:llm 可能是测试里的 SimpleNamespace(无此属性),不可直接取属性。
    claude_config_dir = getattr(llm, "claude_config_dir", "") or ""
    if claude_config_dir:
        env["CLAUDE_CONFIG_DIR"] = claude_config_dir
    return env


def log_gateway_target(agent_name: str, env: dict[str, str], model: str) -> None:
    """
    在真正发起模型调用前,记录本次实际使用的网关目标,便于确认到底用了哪个
    base_url / key(只打印前 8 位前缀)。字段名刻意避开脱敏正则(用 key_head 而非
    api_key=),保证前缀可见落盘。
    """
    base_url = env.get("ANTHROPIC_BASE_URL", "") or "<inherited>"
    api_key = env.get("ANTHROPIC_API_KEY", "") or ""
    key_head = api_key[:8] if api_key else "<none>"
    _logger.info(
        "MODEL_GATEWAY_TARGET agent=%s base_url=%s key_head=%s model=%s",
        agent_name,
        base_url,
        key_head,
        model,
    )


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
        # 最近一次调用的 stderr 捕获器,供上层在超时(协程被取消)后读取尾巴。
        self._last_stderr: Any | None = None

    def _resolve_query_fn(self) -> Callable[..., Any]:
        if self._query_fn is not None:
            return self._query_fn
        # 惰性导入:仅在真正发起调用时才依赖 SDK(其底层会拉起 claude CLI)。
        from claude_agent_sdk import query as sdk_query

        return sdk_query

    def _build_options(self) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        # tools=[] 禁用内建工具:PR2 只做最小模型冒烟,不接任何真实工具。
        stderr_capture = SdkStderrCapture()
        return ClaudeAgentOptions(
            model=self.model,
            env=self.env,
            tools=[],
            stderr=stderr_capture,
            # --debug 让 CLI 把重试原因 / HTTP 状态写到 stderr,配合 INFO 级捕获落盘。
            extra_args={"debug": None},
        )

    async def query(
        self,
        *,
        agent_name: str,
        prompt: str,
        assembled_options: Any | None = None,
    ) -> dict[str, Any]:
        query_fn = self._resolve_query_fn()
        options = assembled_options if assembled_options is not None else self._build_options()

        # 记录本次实际网关目标,并暴露 stderr 捕获器供超时分支读取。
        env = getattr(options, "env", None) or self.env
        log_gateway_target(agent_name, env, getattr(options, "model", self.model))
        self._last_stderr = getattr(options, "stderr", None)

        texts: list[str] = []
        final_text: str | None = None
        usage: dict[str, Any] | None = None
        is_error = False
        error_detail = ""
        stderr_tail = ""

        try:
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
                    diagnostics = sdk_message_diagnostics(message)
                    _logger.info(
                        "MODEL_RESULT_MESSAGE agent=%s detail=%s",
                        agent_name,
                        diagnostics,
                    )
                    is_error = bool(getattr(message, "is_error"))
                    result_text = getattr(message, "result", None)
                    if isinstance(result_text, str):
                        final_text = result_text
                    errors = getattr(message, "errors", None)
                    if errors:
                        error_detail = _format_result_message_error(diagnostics)
                    elif diagnostics:
                        error_detail = _format_result_message_error(diagnostics)
        except Exception as exc:
            stderr_callback = getattr(options, "stderr", None)
            stderr_tail = stderr_callback.tail() if hasattr(stderr_callback, "tail") else ""
            if stderr_tail:
                raise RuntimeCallError(
                    append_stderr_diagnostic(str(exc), stderr_tail)
                ) from exc
            raise

        stderr_callback = getattr(options, "stderr", None)
        stderr_tail = stderr_callback.tail() if hasattr(stderr_callback, "tail") else ""

        if is_error:
            error = RuntimeCallError(
                append_stderr_diagnostic(
                    f"Model reported error for agent={agent_name}: {error_detail or 'unknown'}",
                    stderr_tail,
                )
            )
            # 把已计费 usage 带回上游,避免失败时丢 token / 写假 0。
            error.usage = usage  # type: ignore[attr-defined]
            raise error

        text = final_text if final_text else "".join(texts)
        return {"text": text, "usage": usage}


def sdk_message_diagnostics(message: Any) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {}
    for field in _RESULT_MESSAGE_FIELDS:
        if hasattr(message, field):
            diagnostics[field] = _safe_log_value(getattr(message, field))
    return diagnostics


def _safe_log_value(value: Any) -> Any:
    if isinstance(value, str):
        return _truncate(redact(value), 800)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_safe_log_value(item) for item in value[:10]]
    if isinstance(value, dict):
        return {
            str(key): _safe_log_value(item)
            for key, item in list(value.items())[:20]
        }
    try:
        return _truncate(redact(json.dumps(value, ensure_ascii=False, default=str)), 800)
    except TypeError:
        return _truncate(redact(str(value)), 800)


def _format_result_message_error(diagnostics: dict[str, Any]) -> str:
    parts: list[str] = []
    for key in ("api_error_status", "errors", "stop_reason", "result", "subtype"):
        value = diagnostics.get(key)
        if value not in (None, "", []):
            parts.append(f"{key}={value}")
    return "; ".join(parts)


def _truncate(text: str, limit: int = 800) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "...[truncated]"


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
        request_id = uuid.uuid4().hex[:12]
        _logger.info("MODEL_CALL_START request_id=%s agent=%s model=%s", request_id, agent_name, model)
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
            stderr_callback = getattr(self._client, "_last_stderr", None)
            stderr_tail = stderr_callback.tail() if hasattr(stderr_callback, "tail") else ""
            _logger.error("MODEL_CALL_ERROR request_id=%s agent=%s reason=timeout", request_id, agent_name)
            raise RuntimeTimeoutError(
                append_stderr_diagnostic(
                    f"Runtime call timed out for agent={agent_name}",
                    stderr_tail,
                )
            ) from exc
        except RuntimeCallError as exc:
            _logger.error("MODEL_CALL_ERROR request_id=%s agent=%s reason=%s", request_id, agent_name, exc)
            raise
        except Exception as exc:
            _logger.error("MODEL_CALL_ERROR request_id=%s agent=%s reason=%s", request_id, agent_name, exc)
            raise _classify_runtime_error(exc, agent_name) from exc

        text = _extract_text(raw_response)
        if not text.strip():
            _logger.error("MODEL_CALL_ERROR request_id=%s agent=%s reason=empty_result", request_id, agent_name)
            raise RuntimeCallError(f"Runtime returned empty result for agent={agent_name}")

        usage = extract_usage(raw_response)
        _logger.info(
            "MODEL_CALL_END request_id=%s agent=%s input=%s output=%s cache_creation=%s cache_read=%s",
            request_id,
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
