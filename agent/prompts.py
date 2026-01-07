COMMON_CONSTRAINTS = """
- Focus ONLY on new code added in the PR diff (lines starting with '+').
- You only see changed code hunks (diff hunks), not the entire codebase. Do NOT assume missing context.
- Output MUST be valid YAML and NOTHING ELSE.
- Every multi-line string field MUST use a block scalar with '|' and proper indentation.
- All natural language content (descriptions, analysis, rationale, comments) MUST be in Simplified Chinese.
- 仅输出“高置信度”问题：每条问题必须包含 `confidence`（0-100），并且必须 >= 85；否则不要输出该条。
- 如果未发现置信度 >= 85 的问题：对应列表字段必须输出空数组 `[]`，并明确写“暂未发现高置信度问题”。不要为了凑数而输出低价值建议。
"""

DIFF_FORMAT_NOTE = """
You will receive a PR diff with '+', '-', and ' ' lines. Base findings on '+' lines.
"""

import os

def load_prompt(name: str) -> str:
    """
    Load role prompt markdown and prepend common constraints to improve compliance.
    """
    path = os.path.join("prompt", f"{name}.md")
    if os.path.exists(path):
        with open(path, "r") as f:
            role_prompt = f.read()
        return f"{COMMON_CONSTRAINTS}\n{DIFF_FORMAT_NOTE}\n\n{role_prompt}"
    return f"{COMMON_CONSTRAINTS}\n{DIFF_FORMAT_NOTE}\n\nPrompt {name} not found."

# Dynamic loading
BUSINESS_AGENT_PROMPT = load_prompt("business")
PERFORMANCE_AGENT_PROMPT = load_prompt("performance")
SECURITY_AGENT_PROMPT = load_prompt("security")
TESTING_AGENT_PROMPT = load_prompt("testing")
DOCUMENTATION_AGENT_PROMPT = load_prompt("documentation")
ERROR_HANDLING_AGENT_PROMPT = load_prompt("error_handling")
READABILITY_AGENT_PROMPT = load_prompt("readability")
CONSISTENCY_AGENT_PROMPT = load_prompt("consistency")
MAINTAINABILITY_AGENT_PROMPT = load_prompt("maintainability")
DEPENDENCY_AGENT_PROMPT = load_prompt("dependency")
SUMMARY_AGENT_PROMPT = load_prompt("summary")

ITERATIVE_REVIEW_INSTRUCTION = """
### FOLLOW-UP REVIEW INSTRUCTIONS
Check if previous issues are [FIXED], [PARTIALLY FIXED], or [UNRESOLVED].
"""
