import asyncio
import json
from typing import Dict, Any, List
from .agents import GenericDimensionAgent, QualityLinterAgent, BaseAgent
from .prompts import *
from .utils import build_canonical_summary_payload, parse_summary_llm_output

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
        # Initialize 10 Expert Agents
        self.agents = {
            "business": GenericDimensionAgent(model, BUSINESS_AGENT_PROMPT, "Business", api_base=api_base, **base_agent_kwargs),
            "performance": GenericDimensionAgent(model, PERFORMANCE_AGENT_PROMPT, "Performance", api_base=api_base, **base_agent_kwargs),
            "security": GenericDimensionAgent(model, SECURITY_AGENT_PROMPT, "Security", api_base=api_base, **base_agent_kwargs),
            "testing": GenericDimensionAgent(model, TESTING_AGENT_PROMPT, "Testing", api_base=api_base, **base_agent_kwargs),
            "documentation": GenericDimensionAgent(model, DOCUMENTATION_AGENT_PROMPT, "Documentation", api_base=api_base, **base_agent_kwargs),
            "error_handling": GenericDimensionAgent(model, ERROR_HANDLING_AGENT_PROMPT, "Error Handling", api_base=api_base, **base_agent_kwargs),
            "readability": GenericDimensionAgent(model, READABILITY_AGENT_PROMPT, "Readability", api_base=api_base, **base_agent_kwargs),
            "consistency": QualityLinterAgent(model, api_base=api_base, **base_agent_kwargs),  # Specialized with Linter
            "maintainability": GenericDimensionAgent(model, MAINTAINABILITY_AGENT_PROMPT, "Maintainability", api_base=api_base, **base_agent_kwargs),
            "dependency": GenericDimensionAgent(model, DEPENDENCY_AGENT_PROMPT, "Dependency", api_base=api_base, **base_agent_kwargs)
        }
        self.aggregator = BaseAgent(model, api_base=api_base, **base_agent_kwargs)

    async def _run_with_semaphore(self, semaphore: asyncio.Semaphore, coro):
        async with semaphore:
            return await coro

    async def route_and_aggregate(self, mr_message: str, code_diff: str, file_paths: List[str], project_root: str = ".", language: str = "python", guidelines_path: str = "", requirements_content: str = "", previous_review: Dict[str, Any] = None) -> Dict[str, Any]:
        if previous_review is None: previous_review = {}
        prev_reports = previous_review.get("reports", {})

        print(f"🚀 Starting 10-dimension analysis for MR: {mr_message[:50]}...")
        
        # 统一上下文：包括 MR 信息（标题+描述）和 需求文档内容
        context_info = f"MR TITLE & DESCRIPTION:\n{mr_message}\n\nPRODUCT REQUIREMENTS DOCUMENT:\n{requirements_content}"
        
        # Dispatch 10 agents
        tasks = []
        # Consistency needs file_paths and language
        tasks.append(self.agents["consistency"].run(code_diff, file_paths, project_root, language, guidelines_path, prev_reports.get("consistency", "")))
        
        # Business logic and others use the same context_info
        dims_to_run = ["business", "performance", "security", "testing", "documentation", "error_handling", "readability", "maintainability", "dependency"]
        for dim in dims_to_run:
            tasks.append(self.agents[dim].run(code_diff, context_info, prev_reports.get(dim, ""), language=language))

        # results will be list of (content, usage) tuples
        if self.max_agent_concurrency >= len(tasks):
            results = await asyncio.gather(*tasks)
        else:
            print(f"⚙️ Agent 并发限制已生效: {self.max_agent_concurrency}/{len(tasks)}")
            semaphore = asyncio.Semaphore(self.max_agent_concurrency)
            wrapped_tasks = [self._run_with_semaphore(semaphore, task) for task in tasks]
            results = await asyncio.gather(*wrapped_tasks)
        
        # Aggregate reports and tokens
        report_map = {}
        report_usages = {}
        total_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost": 0.0
        }

        # Order must match tasks.append order
        dims_order = ["consistency", "business", "performance", "security", "testing", "documentation", "error_handling", "readability", "maintainability", "dependency"]
        
        for i, dim in enumerate(dims_order):
            content, usage = results[i]
            report_map[dim] = content
            report_usages[dim] = usage
            # Aggregate usage
            total_usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
            total_usage["completion_tokens"] += usage.get("completion_tokens", 0)
            total_usage["total_tokens"] += usage.get("total_tokens", 0)
            total_usage["cost"] += usage.get("cost", 0.0)

        # --- 新增：打印每个维度报告和 Token 消耗到终端 ---
        for dim, content in report_map.items():
            usage = report_usages.get(dim, {})
            print(f"\n{'='*20} {dim.upper()} REPORT {'='*20}")
            print(f"Token 消耗: {usage.get('total_tokens', 0)} (Input: {usage.get('prompt_tokens', 0)}, Output: {usage.get('completion_tokens', 0)}, Cost: ${usage.get('cost', 0.0):.6f})")
            print(content)
            print(f"{'='*50}\n")
        # ---------------------------------------------------

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
        
        # Aggregate summary tokens
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
            
            # Also include previous line_comments to help identify fixed issues
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
                                body = comment.get("body", "")[:200]  # Limit length
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
