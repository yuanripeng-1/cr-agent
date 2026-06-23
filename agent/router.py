import asyncio
from typing import Dict, Any, List, Tuple, Optional
from .agents import GenericDimensionAgent, BaseAgent
from .prompts import *
from .utils import build_canonical_summary_payload, parse_summary_llm_output


class AgentExecutionContext:
    """封装单次 MR 审查中子 Agent 调用的上下文信息。"""

    def __init__(
        self,
        mr_message: str,
        code_diff: str,
        requirements_content: str,
        previous_review: Dict[str, Any],
    ):
        self.mr_message = mr_message
        self.code_diff = code_diff
        self.requirements_content = requirements_content
        self.previous_review = previous_review or {}
        self.prev_reports = self.previous_review.get("reports", {})

    def build_context_info(self) -> str:
        return (
            f"MR TITLE & DESCRIPTION:\n{self.mr_message}\n\n"
            f"PRODUCT REQUIREMENTS DOCUMENT:\n{self.requirements_content}"
        )


class AgentTaskDescriptor:
    """描述一个待执行的子 Agent 任务。"""

    __slots__ = ("dimension", "coroutine", "priority")

    def __init__(self, dimension: str, coroutine, priority: int = 0):
        self.dimension = dimension
        self.coroutine = coroutine
        self.priority = priority


class AgentResultCollector:
    """
    收集并汇总多个子 Agent 的异步执行结果。
    支持按完成顺序收集，以便在日志中优先展示先完成的维度报告。
    """

    def __init__(self, dimensions: List[str]):
        self.dimensions = dimensions
        self._results: Dict[str, Any] = {}
        self._usages: Dict[str, dict] = {}
        self._completion_order: List[str] = []

    def record_success(self, dimension: str, content: str, usage: dict) -> None:
        self._results[dimension] = content
        self._usages[dimension] = usage
        self._completion_order.append(dimension)

    def record_failure(self, dimension: str, error: Exception) -> None:
        self._results[dimension] = f"Error calling LLM: {repr(error)}"
        self._usages[dimension] = {}
        self._completion_order.append(dimension)
        print(f"❌ 子 Agent [{dimension}] 调用失败: {repr(error)}")

    def aggregate_token_usage(self) -> dict:
        total = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost": 0.0,
        }
        for dim in self.dimensions:
            usage = self._usages.get(dim, {})
            total["prompt_tokens"] += usage.get("prompt_tokens", 0)
            total["completion_tokens"] += usage.get("completion_tokens", 0)
            total["total_tokens"] += usage.get("total_tokens", 0)
            total["cost"] += usage.get("cost", 0.0)
        return total

    def get_report_map(self) -> Dict[str, str]:
        """
        返回维度 -> 报告内容的映射。
        当存在并发完成顺序时，按完成先后与维度列表位置对齐，以便 Summary Agent
        优先看到先完成的高优先级维度输出。
        """
        if not self._completion_order:
            return dict(self._results)
        remapped: Dict[str, str] = {}
        for i, dim in enumerate(self.dimensions):
            if i < len(self._completion_order):
                source_dim = self._completion_order[i]
                remapped[dim] = self._results.get(source_dim, "")
            else:
                remapped[dim] = self._results.get(dim, "")
        return remapped

    def get_report_usages(self) -> Dict[str, dict]:
        return dict(self._usages)

    def print_all_reports(self) -> None:
        for dim in self._completion_order:
            content = self._results.get(dim, "")
            usage = self._usages.get(dim, {})
            print(f"\n{'='*20} {dim.upper()} REPORT {'='*20}")
            print(
                f"Token 消耗: {usage.get('total_tokens', 0)} "
                f"(Input: {usage.get('prompt_tokens', 0)}, "
                f"Output: {usage.get('completion_tokens', 0)}, "
                f"Cost: ${usage.get('cost', 0.0):.6f})"
            )
            print(content)
            print(f"{'='*50}\n")


async def _execute_agent_tasks_with_collector(
    tasks: List[AgentTaskDescriptor],
    collector: AgentResultCollector,
    max_concurrency: int,
    run_with_semaphore_fn,
) -> None:
    """
    执行所有子 Agent 任务并将结果写入 collector。
    统一按完成顺序收集结果，以便日志中优先展示先返回的维度报告。
    """
    if max_concurrency < len(tasks):
        print(f"⚙️ Agent 并发限制已生效: {max_concurrency}/{len(tasks)}")

    semaphore = asyncio.Semaphore(max(1, min(max_concurrency, len(tasks))))

    async def _wrap(descriptor: AgentTaskDescriptor):
        if max_concurrency >= len(tasks):
            result = await descriptor.coroutine
        else:
            result = await run_with_semaphore_fn(semaphore, descriptor.coroutine)
        return descriptor.dimension, result

    wrapped = [_wrap(t) for t in tasks]
    for finished in asyncio.as_completed(wrapped):
        try:
            dim, result = await finished
        except Exception as exc:
            collector.record_failure(tasks[0].dimension, exc)
            continue
        if isinstance(result, Exception):
            collector.record_failure(dim, result)
        else:
            content, usage = result
            collector.record_success(dim, content, usage)

SUMMARY_PARSE_RETRY_INSTRUCTION = (
    "\n\n### RETRY INSTRUCTION\n"
    "上次输出无法被程序解析。请严格修正后重新输出：\n"
    "1. 最终回复的第一个非空字符必须是 `{`，最后一个非空字符必须是 `}`。\n"
    "2. 禁止输出 `json` 前缀、` ```json ` 围栏或任何解释性文字。\n"
    "3. 必须输出合法 JSON，且包含 `llm_result`、`line_comments`、`issues` 三个字段。\n"
    "4. `llm_result` 字符串内的双引号必须转义为 `\\\"`，换行使用 `\\n`。\n"
    "5. `line_comments` 必须是 `{ \"comments\": [...] }` 结构；无评论时输出 `{ \"comments\": [] }`。\n"
)

# 首次请求 + 2 次重试（解析失败时触发）
SUMMARY_PARSE_MAX_ATTEMPTS = 3

# 十个子 Agent 统一处理顺序（与任务派发顺序一致）
SUB_AGENT_DIMS_ORDER = [
    "consistency",
    "business",
    "performance",
    "security",
    "testing",
    "documentation",
    "error_handling",
    "readability",
    "maintainability",
    "dependency",
]

class CRRouter:
    def __init__(
        self,
        model: str = "gpt-4",
        api_base: str | None = None,
        max_agent_concurrency: int = 10,
        timeout_seconds: int | None = None,
        timeout_base_seconds: int = 180,
        timeout_per_1k_chars: int = 1,
        timeout_max_seconds: int = 600,
        max_retries: int = 3,
        retry_delay: float = 2.0,
    ):
        """
        model: 模型名称，如 gpt-4o、qwen-turbo 等。
        api_base: 私有化部署时填写 OpenAI 兼容 API 的 base URL，如 https://your-server/v1（末尾不要带 /chat/completions）。
        """
        self.model = model
        self.api_base = api_base
        self.max_agent_concurrency = max(1, int(max_agent_concurrency))
        base_agent_kwargs = {
            "timeout_seconds": timeout_seconds,
            "timeout_base_seconds": timeout_base_seconds,
            "timeout_per_1k_chars": timeout_per_1k_chars,
            "timeout_max_seconds": timeout_max_seconds,
            "max_retries": max_retries,
            "retry_delay": retry_delay,
        }
        # 十个子 Agent 均使用 GenericDimensionAgent，输入构造方式一致
        self.agents = {
            "business": GenericDimensionAgent(model, BUSINESS_AGENT_PROMPT, "Business", api_base=api_base, **base_agent_kwargs),
            "performance": GenericDimensionAgent(model, PERFORMANCE_AGENT_PROMPT, "Performance", api_base=api_base, **base_agent_kwargs),
            "security": GenericDimensionAgent(model, SECURITY_AGENT_PROMPT, "Security", api_base=api_base, **base_agent_kwargs),
            "testing": GenericDimensionAgent(model, TESTING_AGENT_PROMPT, "Testing", api_base=api_base, **base_agent_kwargs),
            "documentation": GenericDimensionAgent(model, DOCUMENTATION_AGENT_PROMPT, "Documentation", api_base=api_base, **base_agent_kwargs),
            "error_handling": GenericDimensionAgent(model, ERROR_HANDLING_AGENT_PROMPT, "Error Handling", api_base=api_base, **base_agent_kwargs),
            "readability": GenericDimensionAgent(model, READABILITY_AGENT_PROMPT, "Readability", api_base=api_base, **base_agent_kwargs),
            "consistency": GenericDimensionAgent(model, CONSISTENCY_AGENT_PROMPT, "Consistency", api_base=api_base, **base_agent_kwargs),
            "maintainability": GenericDimensionAgent(model, MAINTAINABILITY_AGENT_PROMPT, "Maintainability", api_base=api_base, **base_agent_kwargs),
            "dependency": GenericDimensionAgent(model, DEPENDENCY_AGENT_PROMPT, "Dependency", api_base=api_base, **base_agent_kwargs),
        }
        self.aggregator = BaseAgent(model, api_base=api_base, **base_agent_kwargs)
        self.aggregator.dimension_name = "summary"

    async def _run_with_semaphore(self, semaphore: asyncio.Semaphore, coro):
        async with semaphore:
            return await coro

    async def route_and_aggregate(
        self,
        mr_message: str,
        code_diff: str,
        requirements_content: str = "",
        previous_review: Dict[str, Any] = None,
        language: str = "python",
    ) -> Dict[str, Any]:
        if previous_review is None:
            previous_review = {}

        print(f"🚀 Starting 10-dimension analysis for MR: {mr_message[:50]}...")
        exec_ctx = AgentExecutionContext(
            mr_message, code_diff, requirements_content, previous_review
        )
        context_info = exec_ctx.build_context_info()

        task_descriptors: List[AgentTaskDescriptor] = []
        for dim in SUB_AGENT_DIMS_ORDER:
            task_descriptors.append(
                AgentTaskDescriptor(
                    dimension=dim,
                    coroutine=self.agents[dim].run(
                        code_diff,
                        context_info,
                        exec_ctx.prev_reports.get(dim, ""),
                        language=language,
                    ),
                )
            )

        collector = AgentResultCollector(SUB_AGENT_DIMS_ORDER)
        await _execute_agent_tasks_with_collector(
            task_descriptors,
            collector,
            self.max_agent_concurrency,
            self._run_with_semaphore,
        )

        report_map = collector.get_report_map()
        report_usages = collector.get_report_usages()
        total_usage = collector.aggregate_token_usage()

        collector.print_all_reports()

        print("📝 All expert reports complete. Aggregating...")
        final_summary, summary_usage = await self.generate_final_summary(
            report_map,
            previous_review.get("summary", ""),
            mr_message=mr_message,
            requirements_content=requirements_content,
            code_diff=code_diff,
            previous_review=previous_review,
        )

        print(f"Summary Agent Token 消耗: {summary_usage.get('total_tokens', 0)} (Input: {summary_usage.get('prompt_tokens', 0)}, Output: {summary_usage.get('completion_tokens', 0)}, Cost: ${summary_usage.get('cost', 0.0):.6f})")

        total_usage["prompt_tokens"] += summary_usage.get("prompt_tokens", 0)
        total_usage["completion_tokens"] += summary_usage.get("completion_tokens", 0)
        total_usage["total_tokens"] += summary_usage.get("total_tokens", 0)
        total_usage["cost"] += summary_usage.get("cost", 0.0)

        return {
            "reports": report_map,
            "report_usages": report_usages,
            "summary": final_summary,
            "summary_usage": summary_usage,
            "usage": total_usage,
        }

    async def generate_final_summary(self, report_map: Dict[str, str], previous_summary: str = "", mr_message: str = "", requirements_content: str = "", code_diff: str = "", previous_review: Dict[str, Any] = None) -> tuple[str, dict]:
        system_prompt = SUMMARY_AGENT_PROMPT
        user_prompt = "### MR MESSAGE\n"
        user_prompt += f"{mr_message}\n\n"
        user_prompt += "### PRODUCT REQUIREMENTS DOCUMENT\n"
        user_prompt += f"{requirements_content}\n\n"
        user_prompt += "### CODE DIFF\n"
        user_prompt += f"{code_diff}\n\n"
        user_prompt += "### EXPERT REPORTS (YAML)\n"
        for dim, content in report_map.items():
            user_prompt += f"--- {dim.upper()} REPORT ---\n{content}\n\n"

        if previous_summary:
            user_prompt += f"\n### PREVIOUS REVIEW SUMMARY\n{previous_summary}"

            if previous_review and isinstance(previous_review, dict):
                prev_line_comments = previous_review.get("line_comments", {})
                if prev_line_comments and isinstance(prev_line_comments, dict):
                    prev_comments = prev_line_comments.get("comments", [])
                    if prev_comments and isinstance(prev_comments, list):
                        user_prompt += f"\n### PREVIOUS REVIEW LINE COMMENTS\n"
                        user_prompt += "以下是在上一轮评审中提出的问题（line_comments）：\n\n"
                        for i, comment in enumerate(prev_comments, 1):
                            if isinstance(comment, dict):
                                file_path = comment.get("new_path", "unknown")
                                start_line = comment.get("start_line", "?")
                                end_line = comment.get("end_line", "?")
                                body = comment.get("body", "")[:200]
                                user_prompt += f"{i}. {file_path}:{start_line}-{end_line}\n"
                                user_prompt += f"   问题: {body}\n\n"
                        user_prompt += "\n**重要**：请对比当前 CODE DIFF，如果上述问题已经修复，在增量追踪中标记为 [FIXED]，但不要将其放入新的 line_comments 中。\n"

        total_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost": 0.0,
            "summary_parsed": False,
        }
        last_content = ""
        for attempt in range(1, SUMMARY_PARSE_MAX_ATTEMPTS + 1):
            content, usage = await self.aggregator.call_llm(system_prompt, user_prompt)
            last_content = content
            total_usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
            total_usage["completion_tokens"] += usage.get("completion_tokens", 0)
            total_usage["total_tokens"] += usage.get("total_tokens", 0)
            total_usage["cost"] += usage.get("cost", 0.0)

            parsed_md, parsed_lc, parsed_issues = parse_summary_llm_output(content)
            if parsed_md is not None:
                canonical = build_canonical_summary_payload(parsed_md, parsed_lc, parsed_issues)
                total_usage["retry_count"] = attempt - 1
                total_usage["summary_parsed"] = True
                print(f"✅ Summary 输出解析成功（第 {attempt} 次请求）")
                return canonical, total_usage

            if attempt < SUMMARY_PARSE_MAX_ATTEMPTS:
                print(
                    f"⚠️ Summary 输出解析失败（第 {attempt}/{SUMMARY_PARSE_MAX_ATTEMPTS} 次），"
                    "将重新请求 Summary Agent…"
                )
                user_prompt += SUMMARY_PARSE_RETRY_INSTRUCTION

        total_usage["retry_count"] = SUMMARY_PARSE_MAX_ATTEMPTS - 1
        total_usage["summary_parsed"] = False
        print(
            f"❌ Summary 输出在 {SUMMARY_PARSE_MAX_ATTEMPTS} 次请求后仍无法解析，"
            "将标记为失败"
        )
        return last_content, total_usage
