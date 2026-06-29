"""
main_agent:主 agent 的 agentic 控制流。

控制流反转:不用 Python 顺序链跑 skill,而是把 collect_context / dimension_review /
summarize_report 包装成 SDK 可调用工具,由主 agent 自行规划阶段、并在 validate 失败后
决定是否重调 summarize。orchestrator 退居幕后,只做 max_retries 兜底、usage 汇总、写盘。

=== 重试归属契约(唯一、无二义) ===
- validate_json:纯代码、确定性、**不持有 attempt**、不走 agent、不拿工具。
- 重调由主 agent 决策发起:summarize 工具返回 {valid, errors},主 agent 据此决定是否再调。
- attempt 计数:每次 summarize 工具被调用时 state.attempt += 1(见 summarize_handler)。
- max_retries 兜底(Python 强制终止):can_use_tool 在 summarize 调用前检查,
  允许的 summarize 总次数 = max_retries + 1;超出即 deny,主 agent 无法再重调。

当前 validate 仅做格式校验;若要扩展为行号校验,只需给 validate_json 增加 diff 入参
并改 summarize_handler 这一个调用点 —— 不动上面的状态机。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol

from cr_agent.bootstrap import RuntimeContext
from cr_agent.core.sdk_runtime import (
    build_sdk_env,
    message_text_blocks,
    message_usage,
    sdk_message_diagnostics,
)
from cr_agent.core.review_timing import (
    record_context_skill_s,
    record_main_s,
    record_summary_skill_s,
)
from cr_agent.core.types import TokenUsage, ValidationResult
from cr_agent.core.usage import (
    accumulate_usage,
    accumulate_usage_from_dimension_artifacts,
    coerce_cost,
    extract_usage,
    usage_breakdown_entry,
)
from cr_agent.skills.docs import load_skill_description
from cr_agent.skills.registry import SkillRegistry
from cr_agent.tools.provider import ToolSpec, ok_result
from cr_agent.utils.logging import get_logger

_logger = get_logger("cr_agent.core.main_agent")

# 主 agent 系统提示:只负责阶段规划与 validate 失败后的重调决策,不含工具实现细节。
MAIN_AGENT_SYSTEM_PROMPT = (
    "你是一次代码评审运行的编排 agent。请按顺序调用提供的 skill 工具来规划并执行评审：\n"
    "1. collect_context — 收集评审上下文。\n"
    "2. dimension_review — 按多个维度为变更评分。\n"
    "3. summarize_report — 生成最终报告。它的结果包含 {valid, errors}。"
    "如果 valid 为 false，请判断是否再次调用 summarize_report 来修复已报告的错误。"
    "如果 valid 为 true，则任务完成。\n"
    "不要编造结果；只依赖工具输出。当报告有效，或工具被告知不再允许使用时停止。"
)

# 空入参 schema:skill 不需要主 agent 传参,handler 从 session 取共享状态。
_EMPTY_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}


@dataclass
class MainAgentSession:
    """一次审查运行中,主 agent 与 skill 工具共享的状态。"""

    runtime_context: RuntimeContext
    registry: SkillRegistry
    state: ReviewState
    collected_context: dict[str, Any] | None = None
    dimension_scores: list[dict[str, Any]] | None = None
    last_report: dict[str, Any] | None = None
    last_validation: ValidationResult | None = None

    def add_usage(self, usage: TokenUsage) -> None:
        self.state.tokens_consume = accumulate_usage(self.state.tokens_consume, usage)

    def add_usage_breakdown(self, entry: dict[str, Any]) -> None:
        self.state.token_usage_breakdown.append(entry)


def build_skill_tools(session: MainAgentSession) -> list[ToolSpec]:
    """把 3 个 skill 包装成主 agent 可调用的 ToolSpec。"""

    # 主 Agent 调 skill 时的回调函数
    async def collect_handler(args: dict[str, Any]) -> dict[str, Any]:
        _logger.info("MAIN_AGENT_SKILL_CALL skill=collect_context")
        _logger.info("SKILL_START skill=collect_context")
        skill_start = time.monotonic()
        result = await session.registry.collect_context(session.runtime_context)
        elapsed = time.monotonic() - skill_start
        record_context_skill_s(elapsed)
        usage = result.get("usage")
        if isinstance(usage, TokenUsage):
            session.add_usage(usage)
            session.add_usage_breakdown(
                usage_breakdown_entry(
                    stage="collect_context",
                    skill="collect_context",
                    agent="context",
                    source="skill_result",
                    artifact_path=str(result.get("artifact_path", "")),
                    usage=usage,
                )
            )
        elif isinstance(usage, dict):
            extracted = extract_usage({"usage": usage})
            session.add_usage(extracted)
            session.add_usage_breakdown(
                usage_breakdown_entry(
                    stage="collect_context",
                    skill="collect_context",
                    agent="context",
                    source="skill_result",
                    artifact_path=str(result.get("artifact_path", "")),
                    usage=extracted,
                )
            )
        session.collected_context = result
        _logger.info("SKILL_END skill=collect_context elapsed_s=%.2f", elapsed)
        return ok_result(
            {
                "task_id": result.get("task_id", ""),
                "artifact_path": result.get("artifact_path", ""),
                "summary": result.get("summary", ""),
                "warnings": result.get("warnings", []),
                "status": result.get("status", "success"),
                "error": result.get("error"),
            }
        )

    async def dimension_handler(args: dict[str, Any]) -> dict[str, Any]:
        _logger.info("MAIN_AGENT_SKILL_CALL skill=dimension_review")
        _logger.info("SKILL_START skill=dimension_review")
        if session.collected_context and session.collected_context.get("status") == "failed":
            error = str(session.collected_context.get("error") or "collect_context failed")
            session.state.errors.append(error)
            _logger.warning("SKILL_SKIP skill=dimension_review reason=%s", error)
            return ok_result({"findings": 0, "skipped": True, "error": error})
        skill_start = time.monotonic()
        result = await session.registry.dimension_review(
            session.runtime_context,
            session.collected_context or {},
        )
        elapsed = time.monotonic() - skill_start
        accumulate_usage_from_dimension_artifacts(
            session.runtime_context.result_dir / "dimensions",
            add_usage=session.add_usage,
            add_breakdown=session.add_usage_breakdown,
        )
        session.dimension_scores = result
        _logger.info("SKILL_END skill=dimension_review elapsed_s=%.2f", elapsed)
        return ok_result({"findings": len(result)})

    async def summarize_handler(args: dict[str, Any]) -> dict[str, Any]:
        _logger.info("MAIN_AGENT_SKILL_CALL skill=summarize_report attempt_next=%s", session.state.attempt + 1)
        # attempt 计数唯一落点:每次 summarize 工具被调用即 +1。
        session.state.attempt += 1
        _logger.info("SKILL_START skill=summarize_report attempt=%s", session.state.attempt)
        prior_errors = (
            session.last_validation.errors if session.last_validation is not None else None
        )
        skill_start = time.monotonic()
        report = await session.registry.summarize_report(
            session.runtime_context,
            session.collected_context or {},
            session.dimension_scores or [],
            prior_errors,
        )
        # summary 子 agent 每次调用(含重试)都是真实计费,逐次累加其 usage。
        usage = report.get("usage")
        if isinstance(usage, dict):
            extracted = extract_usage({"usage": usage})
            session.add_usage(extracted)
            session.add_usage_breakdown(
                usage_breakdown_entry(
                    stage="summarize_report",
                    skill="summarize_report",
                    agent="summary",
                    attempt=session.state.attempt,
                    source="skill_result",
                    artifact_path=str(session.runtime_context.result_dir / "summary_report.json"),
                    usage=extracted,
                )
            )
        # validate_json 是纯代码校验,不持有 attempt;结果回给主 agent 决策重调。
        validation = await session.registry.validate_json(session.runtime_context, report)
        elapsed = time.monotonic() - skill_start
        record_summary_skill_s(elapsed)
        session.last_report = report
        session.last_validation = validation
        _logger.info(
            "SKILL_END skill=summarize_report valid=%s errors=%s elapsed_s=%.2f",
            validation.valid,
            len(validation.errors),
            elapsed,
        )
        return ok_result({"valid": validation.valid, "errors": validation.errors})

    return [
        ToolSpec("collect_context", load_skill_description("collect_context"), _EMPTY_SCHEMA, collect_handler),
        ToolSpec("dimension_review", load_skill_description("dimension_review"), _EMPTY_SCHEMA, dimension_handler),
        ToolSpec("summarize_report", load_skill_description("summarize"), _EMPTY_SCHEMA, summarize_handler),
    ]


CanUseTool = Callable[[str, dict[str, Any], Any], Awaitable[Any]]


async def _single_user_prompt(prompt: str) -> AsyncIterator[dict[str, Any]]:
    """
    Claude Agent SDK requires streaming-mode input when can_use_tool is set.
    Wrap the one-shot prompt in the SDK's user-message stream shape.
    """
    yield {
        "type": "user",
        "session_id": "",
        "message": {"role": "user", "content": prompt},
        "parent_tool_use_id": None,
    }


def make_backstop_can_use_tool(session: MainAgentSession) -> CanUseTool:
    """
    Python 侧 max_retries 兜底:summarize 调用前检查 attempt,超过 max_retries+1 即 deny。
    其它工具一律 allow。工具名兼容 SDK 命名空间前缀(mcp__skills__summarize_report)。
    """
    from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

    async def can_use_tool(tool_name: str, tool_input: dict[str, Any], context: Any) -> Any:
        if (
            session.collected_context
            and session.collected_context.get("status") == "failed"
            and (tool_name.endswith("dimension_review") or tool_name.endswith("summarize_report"))
        ):
            error = str(session.collected_context.get("error") or "collect_context failed")
            _logger.warning(
                "DEGRADED reason=COLLECT_CONTEXT_FAILED_DENY_TOOL tool=%s error=%s",
                tool_name,
                error,
            )
            return PermissionResultDeny(message=error)
        if tool_name.endswith("summarize_report"):
            allowed = session.state.max_retries + 1
            if session.state.attempt >= allowed:
                _logger.warning(
                    "DEGRADED reason=MAX_RETRIES tool=%s attempt=%s allowed=%s",
                    tool_name,
                    session.state.attempt,
                    allowed,
                )
                return PermissionResultDeny(message="max_retries exceeded")
        return PermissionResultAllow()

    return can_use_tool


@dataclass
class MainAgentResult:
    text: str = ""
    usage: TokenUsage = field(default_factory=TokenUsage)


class MainAgentRuntime(Protocol):
    async def run_review_loop(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        skill_tools: list[ToolSpec],
        can_use_tool: CanUseTool | None,
        timeout_s: float = 300,
        max_turns: int | None = None,
    ) -> MainAgentResult:
        ...


class SdkMainAgentRuntime:
    """
    真实主 agent runtime:把 skill 工具装进 in-process MCP server,经 Claude Agent SDK
    (统一走 LiteLLM 网关)驱动主 agent。仅在调用时惰性导入 SDK / 拉起 claude CLI;
    单测使用 fake runtime,不进这里。
    """

    def __init__(self, *, model: str, env: dict[str, str] | None = None) -> None:
        self.model = model
        self.env = env or {}

    async def run_review_loop(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        skill_tools: list[ToolSpec],
        can_use_tool: CanUseTool | None,
        timeout_s: float = 300,
        max_turns: int | None = None,
    ) -> MainAgentResult:
        # 惰性导入 claude_agent_sdk:它是可选重依赖(底层会拉起 claude CLI),
        # 只在真正发起主 agent 调用时才需要。这样单测注入 fake runtime 时,
        # 无 SDK 安装的环境也能导入本模块并运行。
        from claude_agent_sdk import (
            ClaudeAgentOptions,
            ResultMessage,
            create_sdk_mcp_server,
            query,
        )

        from cr_agent.core.errors import RuntimeTimeoutError
        from cr_agent.core.sdk_runtime import (
            SdkStderrCapture,
            append_stderr_diagnostic,
            log_gateway_target,
        )
        from cr_agent.core.usage import extract_usage
        from cr_agent.tools.spec_sdk import to_sdk_tool

        '''
        to_sdk_tool(t) → 得到 SdkMcpTool（名字、描述、schema、handler）。
        create_sdk_mcp_server(...) → 把这 3 个 SdkMcpTool 装进一个叫 "skills" 的 进程内 MCP 服务。
        options.mcp_servers={"skills": server} → 告诉 SDK：主 Agent 可以用这些工具。
        模型调 mcp__skills__collect_context 时，SDK 会调 to_sdk_tool 包出来的 handler → 最终到 collect_handler。
        '''
        # 创建 MCP server "skills"，把 skill 工具装进 in-process MCP server
        # to_sdk_tool() 把 ToolSpec.handler 再包一层，转成 SDK 认识的格式；
        server = create_sdk_mcp_server(name="skills", tools=[to_sdk_tool(t) for t in skill_tools])
        allowed = [f"mcp__skills__{t.name}" for t in skill_tools]
        stderr_capture = SdkStderrCapture()

        # 调用SDK的query方法传入的options参数
        options = ClaudeAgentOptions(
            model=self.model,
            env=self.env,
            mcp_servers={"skills": server},
            allowed_tools=allowed,
            can_use_tool=can_use_tool,
            system_prompt=system_prompt,
            tools=[],
            max_turns=max_turns,
            stderr=stderr_capture,
            # --debug 让 CLI 把重试原因 / HTTP 状态写到 stderr,配合 INFO 级捕获落盘。
            extra_args={"debug": None},
        )

        texts: list[str] = []
        final_text: str | None = None
        usage: dict[str, Any] | None = None
        total_cost: float = 0.0
        # 如果can_use_tool不为空，则使用_single_user_prompt包装user_prompt
        prompt = _single_user_prompt(user_prompt) if can_use_tool is not None else user_prompt
        request_id = uuid.uuid4().hex[:12]
        start = time.monotonic()
        log_gateway_target("main", self.env, self.model)
        _logger.info(
            "MODEL_CALL_START request_id=%s agent=main model=%s timeout_s=%s",
            request_id,
            self.model,
            timeout_s,
        )
        try:
            async with asyncio.timeout(timeout_s):
                # 真正启动 Claude Agent SDK 的入口
                # query() 只调用 1 次，内部封装了完整的多轮 agent 循环
                async for message in query(prompt=prompt, options=options):
                    _logger.info(
                        "MODEL_MESSAGE request_id=%s type=%s",
                        request_id,
                        type(message).__name__,
                    )
                    texts.extend(message_text_blocks(message))
                    current_usage = message_usage(message)
                    if current_usage is not None:
                        usage = current_usage
                    # 只有 ResultMessage 携带最终文本与终态;以 isinstance 收敛联合类型分支。
                    if isinstance(message, ResultMessage):
                        _logger.info(
                            "MODEL_RESULT_MESSAGE request_id=%s agent=main detail=%s",
                            request_id,
                            sdk_message_diagnostics(message),
                        )
                        total_cost = coerce_cost(message.total_cost_usd)
                        if isinstance(message.result, str) and message.result:
                            final_text = message.result
        except TimeoutError as exc:
            elapsed = time.monotonic() - start
            _logger.error(
                "MODEL_CALL_ERROR request_id=%s agent=main reason=timeout elapsed_s=%.1f timeout_s=%s",
                request_id,
                elapsed,
                timeout_s,
            )
            raise RuntimeTimeoutError(
                append_stderr_diagnostic(
                    f"Main agent runtime timed out after {elapsed:.1f}s (limit {timeout_s}s)",
                    stderr_capture.tail(),
                )
            ) from exc

        text = final_text if final_text else "".join(texts)
        extracted = extract_usage({"usage": usage, "total_cost_usd": total_cost})
        elapsed = time.monotonic() - start
        record_main_s(elapsed)
        _logger.info(
            "MODEL_CALL_END request_id=%s agent=main elapsed_s=%.1f input=%s output=%s "
            "cache_creation=%s cache_read=%s",
            request_id,
            elapsed,
            extracted.input_tokens,
            extracted.output_tokens,
            extracted.cache_creation_tokens,
            extracted.cache_read_tokens,
        )
        return MainAgentResult(text=text, usage=extracted)


def build_main_agent_runtime(config: Any) -> SdkMainAgentRuntime:
    """从 [llm] 读取 model/api_base/api_key,构造真实主 agent runtime。"""
    llm = config.llm
    return SdkMainAgentRuntime(model=llm.model, env=build_sdk_env(llm))
