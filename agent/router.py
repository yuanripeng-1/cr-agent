import asyncio
import json
from typing import Dict, Any, List
from .agents import GenericDimensionAgent, QualityLinterAgent, BaseAgent
from .prompts import *
try:
    import yaml
except ImportError:
    yaml = None

class CRRouter:
    def __init__(self, model: str = "gpt-4"):
        self.model = model
        # Initialize 10 Expert Agents
        self.agents = {
            "business": GenericDimensionAgent(model, BUSINESS_AGENT_PROMPT, "Business"),
            "performance": GenericDimensionAgent(model, PERFORMANCE_AGENT_PROMPT, "Performance"),
            "security": GenericDimensionAgent(model, SECURITY_AGENT_PROMPT, "Security"),
            "testing": GenericDimensionAgent(model, TESTING_AGENT_PROMPT, "Testing"),
            "documentation": GenericDimensionAgent(model, DOCUMENTATION_AGENT_PROMPT, "Documentation"),
            "error_handling": GenericDimensionAgent(model, ERROR_HANDLING_AGENT_PROMPT, "Error Handling"),
            "readability": GenericDimensionAgent(model, READABILITY_AGENT_PROMPT, "Readability"),
            "consistency": QualityLinterAgent(model), # Specialized with Linter
            "maintainability": GenericDimensionAgent(model, MAINTAINABILITY_AGENT_PROMPT, "Maintainability"),
            "dependency": GenericDimensionAgent(model, DEPENDENCY_AGENT_PROMPT, "Dependency")
        }
        self.aggregator = BaseAgent(model)

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
        results = await asyncio.gather(*tasks)
        
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
            "usage": total_usage
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
            
        def has_markdown_report(content: str) -> bool:
            text = (content or "").strip()
            if not text:
                return False
            if text.startswith("```json"):
                text = text[7:]
            elif text.startswith("```yaml"):
                text = text[7:]
            elif text.startswith("```yml"):
                text = text[6:]
            elif text.startswith("```"):
                text = text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
            try:
                obj = json.loads(text)
                if isinstance(obj, dict) and "markdown_report" in obj:
                    return True
            except Exception:
                pass
            if yaml:
                try:
                    obj = yaml.safe_load(text)
                    if isinstance(obj, dict) and "markdown_report" in obj:
                        return True
                except Exception:
                    pass
            return False

        max_attempts = 2
        total_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "cost": 0.0
        }
        last_content = ""
        for attempt in range(1, max_attempts + 1):
            content, usage = await self.aggregator.call_llm(system_prompt, user_prompt)
            last_content = content
            total_usage["prompt_tokens"] += usage.get("prompt_tokens", 0)
            total_usage["completion_tokens"] += usage.get("completion_tokens", 0)
            total_usage["total_tokens"] += usage.get("total_tokens", 0)
            total_usage["cost"] += usage.get("cost", 0.0)
            if has_markdown_report(content):
                total_usage["retry_count"] = attempt - 1
                return content, total_usage
            if attempt < max_attempts:
                print("⚠️ Summary 输出未包含 markdown_report，触发重试")
                user_prompt += "\n\n### RETRY INSTRUCTION\n上次输出不合规：必须输出 JSON 且包含 markdown_report 与 line_comments，禁止输出 YAML 或其他结构。请严格按照模板生成。"
        total_usage["retry_count"] = max_attempts - 1
        return last_content, total_usage
