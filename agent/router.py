import asyncio
from typing import Dict, Any, List
from .agents import GenericDimensionAgent, QualityLinterAgent, BaseAgent
from .prompts import *

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

        reports = await asyncio.gather(*tasks)
        
        # Map results back
        report_map = {
            "consistency": reports[0],
            "business": reports[1],
            "performance": reports[2],
            "security": reports[3],
            "testing": reports[4],
            "documentation": reports[5],
            "error_handling": reports[6],
            "readability": reports[7],
            "maintainability": reports[8],
            "dependency": reports[9]
        }

        # --- 新增：打印每个维度报告到终端（进而进入 run.log） ---
        for dim, content in report_map.items():
            print(f"\n{'='*20} {dim.upper()} REPORT {'='*20}")
            print(content)
            print(f"{'='*50}\n")
        # ---------------------------------------------------

        print("📝 All expert reports complete. Aggregating...")
        final_summary = await self.generate_final_summary(
            report_map,
            previous_review.get("summary", ""),
            mr_message=mr_message,
            requirements_content=requirements_content,
            code_diff=code_diff,
        )
        
        return {
            "reports": report_map,
            "summary": final_summary
        }

    async def generate_final_summary(self, report_map: Dict[str, str], previous_summary: str = "", mr_message: str = "", requirements_content: str = "", code_diff: str = "") -> str:
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
            
        return await self.aggregator.call_llm(system_prompt, user_prompt)
