from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cr_agent.core.main_agent import MainAgentResult
from cr_agent.core.types import TokenUsage, ValidationResult
from cr_agent.skills.registry import SkillRegistry
from cr_agent.tools.provider import ToolSpec


@dataclass
class ScriptedMainAgentRuntime:
    """
    脚本化的主 agent runtime(测试用),不接真实 SDK / CLI。

    模拟主 agent 的决策:依次调用 collect_context、dimension_review,然后循环调用
    summarize_report,直到 validate 通过或 can_use_tool 拒绝(Python max_retries 兜底)。
    驱动的是 orchestrator 真实构建的 ToolSpec.handler,故 session 状态与生产一致。
    """

    usage: TokenUsage = field(default_factory=TokenUsage)
    summarize_tool_calls: int = 0
    denied: bool = False

    async def run_review_loop(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        skill_tools: list[ToolSpec],
        can_use_tool: Any,
        **kwargs: Any,
    ) -> MainAgentResult:
        tools = {spec.name: spec for spec in skill_tools}

        await self._call(tools["collect_context"], can_use_tool)
        await self._call(tools["dimension_review"], can_use_tool)

        # 主 agent 重调决策:只要校验未过且未被兜底拒绝,就再调一次 summarize。
        while True:
            allow = await can_use_tool("summarize_report", {}, None)
            if getattr(allow, "behavior", "allow") == "deny":
                self.denied = True
                break
            result = await tools["summarize_report"].handler({})
            self.summarize_tool_calls += 1
            data = result.get("data") or {}
            if data.get("valid"):
                break

        return MainAgentResult(text="scripted main done", usage=self.usage)

    async def _call(self, tool: ToolSpec, can_use_tool: Any) -> None:
        await can_use_tool(tool.name, {}, None)
        await tool.handler({})


class FakeSkillScenario:
    def __init__(self, *, validation_failures: int = 0) -> None:
        self.validation_failures = validation_failures
        self.summarize_calls = 0
        self.validate_calls = 0

    def registry(self) -> SkillRegistry:
        return SkillRegistry(
            collect_context=self.collect_context,
            dimension_review=self.dimension_review,
            summarize_report=self.summarize_report,
            validate_json=self.validate_json,
        )

    async def collect_context(self, runtime_context: Any) -> dict[str, Any]:
        return {
            "task_id": runtime_context.review_input.task_id,
            "platform": runtime_context.platform,
        }

    async def dimension_review(
        self,
        runtime_context: Any,
        collected_context: dict[str, Any],
    ) -> list[dict[str, Any]]:
        return [{"dimension": "fake", "score": 100, "findings": []}]

    async def summarize_report(
        self,
        runtime_context: Any,
        collected_context: dict[str, Any],
        dimension_scores: list[dict[str, Any]],
        validation_errors: list[str] | None = None,
    ) -> dict[str, Any]:
        self.summarize_calls += 1
        return {
            "llm_result": (
                "# fake report\n\n"
                f"attempt={self.summarize_calls} "
                f"errors={len(validation_errors or [])}"
            )
        }

    async def validate_json(self, runtime_context: Any, report: dict[str, Any]) -> ValidationResult:
        self.validate_calls += 1
        if self.validate_calls <= self.validation_failures:
            return ValidationResult(valid=False, errors=["invalid fake report"])
        return ValidationResult(valid=True)
