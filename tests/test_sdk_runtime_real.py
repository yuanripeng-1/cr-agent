from __future__ import annotations

from types import SimpleNamespace

import pytest
from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock

from cr_agent.core.errors import RuntimeCallError
from cr_agent.core.orchestrator import run_review
from cr_agent.core.review_output import load_review_result
from cr_agent.core.sdk_runtime import (
    ClaudeAgentRuntime,
    SdkQueryClient,
    build_runtime,
    build_sdk_env,
)


def _result_message(*, result: str, usage: dict | None, is_error: bool = False) -> ResultMessage:
    return ResultMessage(
        subtype="success",
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="sess",
        result=result,
        usage=usage,
        errors=["boom"] if is_error else None,
    )


def _fake_query(messages):
    async def query_fn(*, prompt, options):
        for message in messages:
            yield message

    return query_fn


# ----- build_sdk_env -----

def test_build_sdk_env_maps_base_and_key() -> None:
    llm = SimpleNamespace(api_base="http://gw.local", api_key="sk-secret", model="m")
    env = build_sdk_env(llm)
    assert env == {
        "ANTHROPIC_BASE_URL": "http://gw.local",
        "ANTHROPIC_API_KEY": "sk-secret",
    }


def test_build_sdk_env_omits_empty_values() -> None:
    llm = SimpleNamespace(api_base="", api_key="", model="m")
    assert build_sdk_env(llm) == {}


# ----- build_runtime -----

def test_build_runtime_reads_model_from_llm_config() -> None:
    config = SimpleNamespace(
        llm=SimpleNamespace(model="my-model", api_base="http://gw", api_key="sk-x")
    )
    runtime = build_runtime(config)
    assert isinstance(runtime, ClaudeAgentRuntime)
    assert isinstance(runtime._client, SdkQueryClient)
    assert runtime._client.model == "my-model"
    assert runtime._client.env["ANTHROPIC_API_KEY"] == "sk-x"


# ----- SdkQueryClient message parsing via injected fake query_fn -----

@pytest.mark.asyncio
async def test_sdk_client_extracts_text_and_four_usage_fields() -> None:
    messages = [
        AssistantMessage(content=[TextBlock(text="partial ")], model="m"),
        _result_message(
            result="final answer",
            usage={
                "input_tokens": 11,
                "output_tokens": 22,
                "cache_creation_input_tokens": 3,
                "cache_read_input_tokens": 4,
            },
        ),
    ]
    client = SdkQueryClient(model="m", query_fn=_fake_query(messages))
    runtime = ClaudeAgentRuntime(config={}, client=client)

    result = await runtime.query_main("hi")
    assert result.text == "final answer"
    assert result.usage.input_tokens == 11
    assert result.usage.output_tokens == 22
    assert result.usage.cache_creation_tokens == 3
    assert result.usage.cache_read_tokens == 4


@pytest.mark.asyncio
async def test_sdk_client_falls_back_to_assistant_text_when_no_result() -> None:
    messages = [
        AssistantMessage(content=[TextBlock(text="hello "), TextBlock(text="world")], model="m"),
        _result_message(result="", usage={"input_tokens": 1, "output_tokens": 2}),
    ]
    client = SdkQueryClient(model="m", query_fn=_fake_query(messages))
    runtime = ClaudeAgentRuntime(config={}, client=client)

    result = await runtime.query_main("hi")
    assert result.text == "hello world"


@pytest.mark.asyncio
async def test_sdk_client_raises_with_usage_on_model_error() -> None:
    messages = [
        _result_message(
            result="",
            usage={"input_tokens": 5, "output_tokens": 0},
            is_error=True,
        ),
    ]
    client = SdkQueryClient(model="m", query_fn=_fake_query(messages))

    with pytest.raises(RuntimeCallError) as excinfo:
        await client.query(agent_name="main", prompt="hi")
    # 计费 usage 带回上游,失败也不丢 token。
    assert excinfo.value.usage == {"input_tokens": 5, "output_tokens": 0}


@pytest.mark.asyncio
async def test_runtime_rejects_empty_text_from_real_client_path() -> None:
    messages = [_result_message(result="", usage=None)]
    client = SdkQueryClient(model="m", query_fn=_fake_query(messages))
    runtime = ClaudeAgentRuntime(config={}, client=client)

    with pytest.raises(RuntimeCallError, match="empty result"):
        await runtime.query_main("hi")


# ----- orchestrator preserves accumulated token on failure (no fake 0) -----

class _FailingRuntimeWithUsage:
    async def query_main(self, prompt: str, **kwargs):
        error = RuntimeCallError("model errored after billing")
        error.usage = {  # type: ignore[attr-defined]
            "input_tokens": 9,
            "output_tokens": 1,
            "cache_creation_input_tokens": 2,
            "cache_read_input_tokens": 3,
        }
        raise error


@pytest.mark.asyncio
async def test_run_review_writes_real_accumulated_tokens_on_failure(agent_config_path) -> None:
    from cr_agent.bootstrap import bootstrap_runtime

    runtime_context = bootstrap_runtime(agent_config_path, platform_override=None)
    result = await run_review(runtime_context, agent_runtime=_FailingRuntimeWithUsage())

    assert result.status == "error"
    written = load_review_result(runtime_context.result_dir / "result.json")
    # 失败产物写真实已累计 token,而非假 0。
    assert written.tokens_consume.input_tokens == 9
    assert written.tokens_consume.output_tokens == 1
