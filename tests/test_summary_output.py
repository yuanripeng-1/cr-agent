"""
tests/test_summary_output.py

覆盖三大模块的测试：
1. SummaryOutput Pydantic 模型与 validate_summary_output 工具函数
2. validate_json skill 的分阶段校验（含 SummaryOutput 前置校验）
3. ClaudeAgentRuntime.query_subagent_structured() 的结构化输出与失败禁用逻辑
4. summarize skill 调用 query_subagent_structured 的行为
"""

from __future__ import annotations

import types
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cr_agent.bootstrap import bootstrap_runtime
from cr_agent.core.summary_output import (
    SUMMARY_JSON_SCHEMA,
    SummaryOutput,
    validate_summary_output,
)
from cr_agent.core.types import QueryResult
from cr_agent.skills.summarize.skill import invoke_summary_agent, summarize_report
from cr_agent.skills.validate_json.skill import validate_json


# ─────────────────────────────────────────────────────────────────────────────
# 辅助数据
# ─────────────────────────────────────────────────────────────────────────────

_VALID_REPORT: dict[str, Any] = {
    "llm_result": "# 代码评审报告\n\n## 总结\n...",
    "line_comments": {
        "comments": [
            {
                "new_path": "src/main.py",
                "body": "🔴 Critical｜高风险，建议修复后再合入\n\n**SQL 注入漏洞**",
                "start_line": 42,
                "end_line": 42,
            }
        ]
    },
    "issues": [
        {
            "severity": "critical",
            "title": "SQL 注入漏洞",
            "count": 1,
            "locations": [{"path": "src/main.py", "start_line": 42, "end_line": 42}],
        }
    ],
}

_EMPTY_ISSUES_REPORT: dict[str, Any] = {
    "llm_result": "# 代码评审报告\n\n无问题",
    "line_comments": {"comments": []},
    "issues": [],
}


# ─────────────────────────────────────────────────────────────────────────────
# 第一组：SummaryOutput Pydantic 模型
# ─────────────────────────────────────────────────────────────────────────────


class TestSummaryOutputModel:
    def test_valid_full_report_passes(self) -> None:
        obj = SummaryOutput.model_validate(_VALID_REPORT)
        assert obj.llm_result.startswith("# 代码评审报告")
        assert len(obj.line_comments.comments) == 1
        assert len(obj.issues) == 1

    def test_valid_empty_issues_and_comments_passes(self) -> None:
        obj = SummaryOutput.model_validate(_EMPTY_ISSUES_REPORT)
        assert obj.issues == []
        assert obj.line_comments.comments == []

    def test_missing_llm_result_fails(self) -> None:
        data = {k: v for k, v in _VALID_REPORT.items() if k != "llm_result"}
        errors = validate_summary_output(data)
        assert errors
        assert any("llm_result" in e for e in errors)

    def test_empty_llm_result_fails(self) -> None:
        data = {**_VALID_REPORT, "llm_result": ""}
        errors = validate_summary_output(data)
        assert errors
        assert any("llm_result" in e for e in errors)

    def test_missing_line_comments_fails(self) -> None:
        data = {k: v for k, v in _VALID_REPORT.items() if k != "line_comments"}
        errors = validate_summary_output(data)
        assert errors
        assert any("line_comments" in e for e in errors)

    def test_missing_comments_key_in_line_comments_fails(self) -> None:
        data = {**_VALID_REPORT, "line_comments": {"extra": "value"}}
        errors = validate_summary_output(data)
        assert errors
        assert any("comments" in e for e in errors)

    def test_missing_issues_fails(self) -> None:
        data = {k: v for k, v in _VALID_REPORT.items() if k != "issues"}
        errors = validate_summary_output(data)
        assert errors
        assert any("issues" in e for e in errors)

    def test_invalid_severity_fails(self) -> None:
        data = {
            **_VALID_REPORT,
            "issues": [
                {**_VALID_REPORT["issues"][0], "severity": "blocker"}
            ],
        }
        errors = validate_summary_output(data)
        assert errors
        assert any("severity" in e for e in errors)

    def test_missing_comment_body_fails(self) -> None:
        data = {
            **_VALID_REPORT,
            "line_comments": {
                "comments": [
                    {
                        "new_path": "src/main.py",
                        "start_line": 1,
                        "end_line": 1,
                        # body 缺失
                    }
                ]
            },
        }
        errors = validate_summary_output(data)
        assert errors
        assert any("body" in e for e in errors)

    def test_missing_issue_location_path_fails(self) -> None:
        data = {
            **_VALID_REPORT,
            "issues": [
                {
                    **_VALID_REPORT["issues"][0],
                    "locations": [{"start_line": 1, "end_line": 1}],  # path 缺失
                }
            ],
        }
        errors = validate_summary_output(data)
        assert errors
        assert any("path" in e for e in errors)

    def test_extra_fields_allowed(self) -> None:
        data = {**_VALID_REPORT, "extra_diagnostic": "some value"}
        errors = validate_summary_output(data)
        assert errors == []

    def test_validate_summary_output_returns_empty_on_valid(self) -> None:
        errors = validate_summary_output(_VALID_REPORT)
        assert errors == []

    def test_validate_summary_output_returns_empty_for_empty_issues(self) -> None:
        errors = validate_summary_output(_EMPTY_ISSUES_REPORT)
        assert errors == []

    def test_summary_json_schema_has_required_fields(self) -> None:
        schema = SUMMARY_JSON_SCHEMA
        assert schema["name"] == "summary_output"
        required = schema["schema"]["required"]
        assert "llm_result" in required
        assert "line_comments" in required
        assert "issues" in required


# ─────────────────────────────────────────────────────────────────────────────
# 第二组：enhanced validate_json skill（分阶段校验）
# ─────────────────────────────────────────────────────────────────────────────


class TestEnhancedValidateJson:
    @pytest.mark.asyncio
    async def test_accepts_complete_valid_report(self, agent_config_path: Path) -> None:
        ctx = bootstrap_runtime(agent_config_path, platform_override=None)
        result = await validate_json(ctx, _EMPTY_ISSUES_REPORT)
        assert result.valid is True
        assert result.errors == []

    @pytest.mark.asyncio
    async def test_rejects_missing_line_comments(self, agent_config_path: Path) -> None:
        ctx = bootstrap_runtime(agent_config_path, platform_override=None)
        data = {k: v for k, v in _VALID_REPORT.items() if k != "line_comments"}
        result = await validate_json(ctx, data)
        assert result.valid is False
        assert any("line_comments" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_rejects_missing_issues(self, agent_config_path: Path) -> None:
        ctx = bootstrap_runtime(agent_config_path, platform_override=None)
        data = {k: v for k, v in _VALID_REPORT.items() if k != "issues"}
        result = await validate_json(ctx, data)
        assert result.valid is False
        assert any("issues" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_rejects_empty_llm_result_via_summary_output(
        self, agent_config_path: Path
    ) -> None:
        ctx = bootstrap_runtime(agent_config_path, platform_override=None)
        data = {**_EMPTY_ISSUES_REPORT, "llm_result": ""}
        result = await validate_json(ctx, data)
        assert result.valid is False
        # SummaryOutput 第一阶段会捕获 llm_result 错误
        assert any("llm_result" in e for e in result.errors)

    @pytest.mark.asyncio
    async def test_accumulates_errors_from_all_stages(
        self, agent_config_path: Path
    ) -> None:
        """缺失 issues 且 llm_result 为空——两阶段的错误都应出现。"""
        ctx = bootstrap_runtime(agent_config_path, platform_override=None)
        data = {
            "llm_result": "",
            "line_comments": {"comments": []},
            # issues 缺失
        }
        result = await validate_json(ctx, data)
        assert result.valid is False
        # SummaryOutput 阶段捕获 llm_result 和 issues 错误
        assert any("llm_result" in e for e in result.errors)
        assert any("issues" in e for e in result.errors)


# ─────────────────────────────────────────────────────────────────────────────
# 第三组：ClaudeAgentRuntime.query_subagent_structured
# ─────────────────────────────────────────────────────────────────────────────


def _make_litellm_response(content: str) -> MagicMock:
    """构造一个模拟 litellm.acompletion() 响应对象。"""
    message = MagicMock()
    message.content = content
    choice = MagicMock()
    choice.message = message
    usage = MagicMock()
    usage.prompt_tokens = 100
    usage.completion_tokens = 50
    resp = MagicMock()
    resp.choices = [choice]
    resp.usage = usage
    return resp


def _make_runtime(model: str = "qwen-plus", api_base: str = "http://127.0.0.1:4000") -> Any:
    """构造一个带 llm config 的 ClaudeAgentRuntime（fake client，不联网）。"""
    from cr_agent.core.sdk_runtime import ClaudeAgentRuntime

    llm_config = types.SimpleNamespace(
        model=model,
        api_base=api_base,
        api_key="sk-test",
        gateway_provider="openai",
    )
    config = types.SimpleNamespace(llm=llm_config)

    class _FakeClient:
        async def query(self, *, agent_name, prompt, assembled_options=None):
            return {"text": '{"fallback": true}', "usage": None}

    return ClaudeAgentRuntime(config=config, client=_FakeClient())


class TestQuerySubagentStructured:
    @pytest.mark.asyncio
    async def test_calls_litellm_with_response_format(self) -> None:
        runtime = _make_runtime()
        response_text = '{"llm_result": "# ok", "line_comments": {"comments": []}, "issues": []}'
        mock_response = _make_litellm_response(response_text)

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_litellm:
            mock_litellm.return_value = mock_response
            result = await runtime.query_subagent_structured(
                "summary",
                "test prompt",
                response_schema=SUMMARY_JSON_SCHEMA,
                timeout_s=30,
            )

        assert result.text == response_text
        assert result.usage.input_tokens == 100
        assert result.usage.output_tokens == 50
        # 验证 response_format 被正确传递
        call_kwargs = mock_litellm.call_args.kwargs
        assert call_kwargs["response_format"]["type"] == "json_schema"
        assert call_kwargs["response_format"]["json_schema"] == SUMMARY_JSON_SCHEMA
        assert call_kwargs["model"] == "openai/qwen-plus"
        assert call_kwargs["api_base"] == "http://127.0.0.1:4000"

    @pytest.mark.asyncio
    async def test_raises_when_litellm_raises_generic_error_without_disabling(self) -> None:
        from cr_agent.core.errors import StructuredOutputError

        runtime = _make_runtime()

        with patch("litellm.acompletion", side_effect=RuntimeError("provider error")):
            with pytest.raises(StructuredOutputError, match="structured output failed") as raised:
                await runtime.query_subagent_structured(
                    "summary",
                    "test prompt",
                    response_schema=SUMMARY_JSON_SCHEMA,
                    timeout_s=30,
                )

        assert raised.value.kind == "error"
        assert runtime.structured_output_disabled is False

    @pytest.mark.asyncio
    async def test_disables_when_litellm_reports_unsupported(self) -> None:
        from cr_agent.core.errors import StructuredOutputError

        runtime = _make_runtime()

        with patch(
            "litellm.acompletion",
            side_effect=RuntimeError("response_format json_schema is not supported"),
        ):
            with pytest.raises(StructuredOutputError, match="structured output failed") as raised:
                await runtime.query_subagent_structured(
                    "summary",
                    "test prompt",
                    response_schema=SUMMARY_JSON_SCHEMA,
                    timeout_s=30,
                )

        assert raised.value.kind == "unsupported"
        assert runtime.structured_output_disabled is True
        assert runtime.last_structured_output_error is not None

    @pytest.mark.asyncio
    async def test_falls_back_when_schema_is_none(self) -> None:
        runtime = _make_runtime()

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_litellm:
            result = await runtime.query_subagent_structured(
                "summary",
                "test prompt",
                response_schema=None,
                timeout_s=30,
            )

        # response_schema=None 直接走 query_subagent()，不调 litellm
        mock_litellm.assert_not_called()
        assert "fallback" in result.text

    @pytest.mark.asyncio
    async def test_falls_back_when_api_base_missing(self) -> None:
        from cr_agent.core.sdk_runtime import ClaudeAgentRuntime

        llm_config = types.SimpleNamespace(
            model="qwen-plus",
            api_base="",  # 空 api_base
            api_key="sk-test",
            gateway_provider="openai",
        )
        config = types.SimpleNamespace(llm=llm_config)

        class _FakeClient:
            async def query(self, *, agent_name, prompt, assembled_options=None):
                return {"text": '{"fallback_missing_base": true}', "usage": None}

        runtime = ClaudeAgentRuntime(config=config, client=_FakeClient())

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_litellm:
            result = await runtime.query_subagent_structured(
                "summary",
                "test prompt",
                response_schema=SUMMARY_JSON_SCHEMA,
                timeout_s=30,
            )

        mock_litellm.assert_not_called()
        assert "fallback_missing_base" in result.text

    @pytest.mark.asyncio
    async def test_raises_when_response_is_empty(self) -> None:
        from cr_agent.core.errors import StructuredOutputError

        runtime = _make_runtime()
        mock_response = _make_litellm_response("")  # 空响应

        with patch("litellm.acompletion", new_callable=AsyncMock) as mock_litellm:
            mock_litellm.return_value = mock_response
            with pytest.raises(StructuredOutputError, match="empty content") as raised:
                await runtime.query_subagent_structured(
                    "summary",
                    "test prompt",
                    response_schema=SUMMARY_JSON_SCHEMA,
                    timeout_s=30,
                )

        assert raised.value.kind == "empty"
        assert runtime.structured_output_disabled is False

    @pytest.mark.asyncio
    async def test_raises_when_litellm_times_out_without_disabling(self) -> None:
        """litellm 超时应抛出 StructuredOutputError(kind=timeout)，且不禁用 structured。"""
        import asyncio

        from cr_agent.core.errors import StructuredOutputError

        runtime = _make_runtime()

        async def _slow(*args, **kwargs):
            await asyncio.sleep(999)

        with patch("litellm.acompletion", side_effect=_slow):
            with pytest.raises(StructuredOutputError, match="timed out") as raised:
                await runtime.query_subagent_structured(
                    "summary",
                    "test prompt",
                    response_schema=SUMMARY_JSON_SCHEMA,
                    timeout_s=0.001,
                )

        assert raised.value.kind == "timeout"
        assert runtime.structured_output_disabled is False


# ─────────────────────────────────────────────────────────────────────────────
# 第四组：summarize skill 使用 query_subagent_structured
# ─────────────────────────────────────────────────────────────────────────────


class _StructuredRuntime:
    """同时实现 query_subagent 和 query_subagent_structured 的测试 fake。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.structured_calls: list[dict] = []
        self.fallback_calls: list[dict] = []

    async def query_subagent(self, agent_name, prompt, *, assembled_options=None, timeout_s=None):
        self.fallback_calls.append({"agent_name": agent_name, "prompt": prompt})
        return QueryResult(text=self.text)

    async def query_subagent_structured(
        self, agent_name, prompt, *, response_schema=None, timeout_s=None
    ):
        self.structured_calls.append(
            {"agent_name": agent_name, "prompt": prompt, "response_schema": response_schema}
        )
        return QueryResult(text=self.text)


class TestSummarizeUsesStructuredOutput:
    @pytest.mark.asyncio
    async def test_uses_query_subagent_structured_when_available(
        self, agent_config_path: Path
    ) -> None:
        """summarize skill 在 runtime 支持 query_subagent_structured 时应优先使用它。"""
        runtime = _StructuredRuntime(
            '{"llm_result": "# report", "line_comments": {"comments": []}, "issues": []}'
        )
        ctx = replace(
            bootstrap_runtime(agent_config_path, platform_override=None),
            summary_runtime=runtime,
        )

        report = await summarize_report(ctx, {"task_id": "task-1"}, [], None)

        assert len(runtime.structured_calls) == 1
        assert len(runtime.fallback_calls) == 0
        assert runtime.structured_calls[0]["agent_name"] == "summary"
        assert runtime.structured_calls[0]["response_schema"] == SUMMARY_JSON_SCHEMA
        assert report["llm_result"] == "# report"

    @pytest.mark.asyncio
    async def test_falls_back_to_query_subagent_when_method_absent(
        self, agent_config_path: Path
    ) -> None:
        """无 query_subagent_structured 的旧 fake runtime 仍走 query_subagent。"""

        class _OldRuntime:
            def __init__(self) -> None:
                self.calls: list[dict] = []

            async def query_subagent(
                self, agent_name, prompt, *, assembled_options=None, timeout_s=None
            ):
                self.calls.append({"agent_name": agent_name})
                return QueryResult(
                    text='{"llm_result": "# old", "line_comments": {"comments": []}, "issues": []}'
                )

        runtime = _OldRuntime()
        ctx = replace(
            bootstrap_runtime(agent_config_path, platform_override=None),
            summary_runtime=runtime,
        )

        report = await summarize_report(ctx, {"task_id": "task-1"}, [], None)

        assert len(runtime.calls) == 1
        assert runtime.calls[0]["agent_name"] == "summary"
        assert report["llm_result"] == "# old"

    @pytest.mark.asyncio
    async def test_retries_with_query_subagent_after_unsupported_structured_failure(
        self, agent_config_path: Path, tmp_path: Path
    ) -> None:
        """模型不支持 structured 时，第二次 summarize 应走 query_subagent。"""
        from cr_agent.core.errors import StructuredOutputError

        class _StructuredThenPlainRuntime:
            def __init__(self) -> None:
                self.structured_output_disabled = False
                self.last_structured_output_error: str | None = None
                self.structured_calls = 0
                self.plain_calls = 0

            async def query_subagent_structured(
                self, agent_name, prompt, *, response_schema=None, timeout_s=None
            ):
                self.structured_calls += 1
                self.structured_output_disabled = True
                self.last_structured_output_error = "response_format not supported"
                raise StructuredOutputError(
                    "response_format json_schema is not supported",
                    kind="unsupported",
                )

            async def query_subagent(
                self, agent_name, prompt, *, assembled_options=None, timeout_s=None
            ):
                self.plain_calls += 1
                return QueryResult(
                    text='{"llm_result": "# plain", "line_comments": {"comments": []}, "issues": []}'
                )

        runtime = _StructuredThenPlainRuntime()
        ctx = replace(
            bootstrap_runtime(agent_config_path, platform_override=None),
            summary_runtime=runtime,
            result_dir=tmp_path,
        )

        first = await summarize_report(ctx, {"task_id": "task-1"}, [], None)
        failure_txt = (tmp_path / "summary_report.txt").read_text(encoding="utf-8")
        assert "kind=unsupported" in failure_txt

        second = await summarize_report(ctx, {"task_id": "task-1"}, [], ["llm_result: Field required"])

        assert runtime.structured_calls == 1
        assert runtime.plain_calls == 1
        assert first["structured_output_error_kind"] == "unsupported"
        assert first["llm_result"] == ""
        assert second["llm_result"] == "# plain"
        assert '"llm_result": "# plain"' in (tmp_path / "summary_report.txt").read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_retries_with_structured_after_timeout_failure(
        self, agent_config_path: Path, tmp_path: Path
    ) -> None:
        """structured 超时时，第二次 summarize 仍应尝试 structured。"""
        from cr_agent.core.errors import StructuredOutputError

        class _TimeoutThenStructuredRuntime:
            def __init__(self) -> None:
                self.structured_output_disabled = False
                self.last_structured_output_error: str | None = None
                self.structured_calls = 0
                self.plain_calls = 0

            async def query_subagent_structured(
                self, agent_name, prompt, *, response_schema=None, timeout_s=None
            ):
                self.structured_calls += 1
                if self.structured_calls == 1:
                    raise StructuredOutputError("structured output timed out", kind="timeout")
                return QueryResult(
                    text='{"llm_result": "# structured retry", "line_comments": {"comments": []}, "issues": []}'
                )

            async def query_subagent(
                self, agent_name, prompt, *, assembled_options=None, timeout_s=None
            ):
                self.plain_calls += 1
                return QueryResult(text='{"llm_result": "# plain"}')

        runtime = _TimeoutThenStructuredRuntime()
        ctx = replace(
            bootstrap_runtime(agent_config_path, platform_override=None),
            summary_runtime=runtime,
            result_dir=tmp_path,
        )

        first = await summarize_report(ctx, {"task_id": "task-1"}, [], None)
        second = await summarize_report(ctx, {"task_id": "task-1"}, [], ["llm_result: Field required"])

        assert runtime.structured_calls == 2
        assert runtime.plain_calls == 0
        assert first["structured_output_error_kind"] == "timeout"
        assert second["llm_result"] == "# structured retry"

    @pytest.mark.asyncio
    async def test_uses_query_subagent_when_structured_output_disabled_in_config(
        self, agent_config_path: Path, tmp_path: Path
    ) -> None:
        """[llm].summary_structured_output=false 时应走 query_subagent，不调 structured。"""
        runtime = _StructuredRuntime(
            '{"llm_result": "# sdk", "line_comments": {"comments": []}, "issues": []}'
        )
        base_ctx = bootstrap_runtime(agent_config_path, platform_override=None)
        config = base_ctx.config.model_copy(
            update={
                "llm": base_ctx.config.llm.model_copy(
                    update={"summary_structured_output": False}
                )
            }
        )
        ctx = replace(
            base_ctx,
            summary_runtime=runtime,
            config=config,
            result_dir=tmp_path,
        )

        report = await summarize_report(ctx, {"task_id": "task-1"}, [], None)

        assert len(runtime.structured_calls) == 0
        assert len(runtime.fallback_calls) == 1
        assert report["llm_result"] == "# sdk"


class TestInvokeSummaryAgent:
    @pytest.mark.asyncio
    async def test_writes_to_custom_output_paths(
        self, agent_config_path: Path, tmp_path: Path
    ) -> None:
        runtime = _StructuredRuntime(
            '{"llm_result": "# custom", "line_comments": {"comments": []}, "issues": []}'
        )
        base_ctx = bootstrap_runtime(agent_config_path, platform_override=None)
        config = base_ctx.config.model_copy(
            update={
                "llm": base_ctx.config.llm.model_copy(
                    update={"summary_structured_output": False}
                )
            }
        )
        custom_txt = tmp_path / "out" / "report.txt"
        custom_json = tmp_path / "out" / "report.json"
        ctx = replace(
            base_ctx,
            summary_runtime=runtime,
            config=config,
            result_dir=tmp_path,
        )

        report = await invoke_summary_agent(
            ctx,
            "test prompt",
            summary_report_txt_path=custom_txt,
            summary_report_json_path=custom_json,
        )

        assert report["llm_result"] == "# custom"
        assert custom_txt.read_text(encoding="utf-8").startswith("{")
        assert custom_json.exists()
