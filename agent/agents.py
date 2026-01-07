import subprocess
import os
import litellm
from typing import List, Dict, Any
from .prompts import *

class BaseAgent:
    def __init__(self, model: str = "gpt-4"):
        self.model = model

    async def call_llm(self, system_prompt: str, user_prompt: str) -> str:
        try:
            response = await litellm.acompletion(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=0.2
            )
            return response.choices[0].message.content
        except Exception as e:
            return f"Error calling LLM: {str(e)}"

class GenericDimensionAgent(BaseAgent):
    """A generic agent for single-dimension review."""
    def __init__(self, model: str, system_prompt: str, dimension_name: str):
        super().__init__(model)
        self.system_prompt = system_prompt
        self.dimension_name = dimension_name

    async def run(self, code_diff: str, context_info: str = "", previous_review: str = "", language: str = "python") -> str:
        # Language-Specific Red Flags
        lang_checks = {
            "python": "- Watch for: Mutable default arguments, broad except: pass, circular imports, misuse of *args/**kwargs.",
            "go": "- Watch for: Unclosed channels, nil map assignment, goroutine leaks, error ignoring (_), interface pollution.",
            "java": "- Watch for: NullPointerExceptions, raw types, memory leaks in static maps, swallowing exceptions.",
            "javascript": "- Watch for: '==', callback hell, unhandled promises, global variable pollution, 'any' type in TS.",
            "typescript": "- Watch for: 'any' type usage, non-null assertions (!), ignore comments.",
            "node": "- Watch for: Blocking I/O in event loop, unhandled promise rejections, console.log left in production."
        }
        
        specific_instruction = lang_checks.get(language.lower(), "")
        
        user_prompt = f"### CODE DIFF\n{code_diff}\n"
        
        if specific_instruction:
            user_prompt += f"\n### LANGUAGE SPECIFIC CHECKS ({language})\n{specific_instruction}\n"
            
        if context_info:
            user_prompt += f"\n### CONTEXT INFO\n{context_info}\n"
            
        if previous_review:
            user_prompt += f"\n\n{ITERATIVE_REVIEW_INSTRUCTION}\n\n### PREVIOUS REVIEW REPORT ({self.dimension_name})\n{previous_review}"
        
        # Anti-Hallucination Injection (High-confidence only)
        user_prompt += "\n\nIMPORTANT: 仅输出置信度>=85的问题；每条必须带confidence字段(0-100)。如果没有高置信度问题，输出空数组并写明“暂未发现高置信度问题”。严禁为了凑数输出低价值建议。"
        
        return await self.call_llm(self.system_prompt, user_prompt)

class QualityLinterAgent(BaseAgent):
    """Consistency & Style Agent that also runs physical Linter."""
    async def run(self, code_diff: str, file_paths: List[str], project_root: str = ".", language: str = "python", guidelines_path: str = "", previous_review: str = "") -> str:
        # Linter execution logic
        linter_output = ""
        linter_cmd = []
        if language.lower() == "python":
            linter_cmd = ["pylint", "--output-format=text"]
        elif language.lower() == "go":
            linter_cmd = ["golangci-lint", "run"]
        
        if linter_cmd:
            for path in file_paths:
                try:
                    cmd = linter_cmd + [path]
                    process = subprocess.run(cmd, cwd=project_root, capture_output=True, text=True, timeout=30)
                    linter_output += f"\nFile: {path}\n{process.stdout}\n{process.stderr}"
                except Exception as e:
                    linter_output += f"\nLinter Error on {path}: {str(e)}"

        # Guidelines
        guidelines_content = "No specific guidelines provided."
        if guidelines_path:
            lang_map = {"python": "python_style.md", "go": "go_style.md", "java": "java_style.md", "javascript": "nodejs_style.md"}
            fname = lang_map.get(language.lower())
            if fname:
                full_path = os.path.join(guidelines_path, fname)
                if os.path.exists(full_path):
                    with open(full_path, "r") as f: guidelines_content = f.read()

        user_prompt = (
            f"### TEAM CODING GUIDELINES\n{guidelines_content}\n\n"
            f"### LINTER OUTPUT\n{linter_output}\n\n"
            f"### CODE DIFF\n{code_diff}\n"
        )
        if previous_review:
            user_prompt += f"\n\n{ITERATIVE_REVIEW_INSTRUCTION}\n\n### PREVIOUS REVIEW REPORT\n{previous_review}"
        
        return await self.call_llm(CONSISTENCY_AGENT_PROMPT, user_prompt)
