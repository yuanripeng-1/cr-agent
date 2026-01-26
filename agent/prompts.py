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
### FOLLOW-UP REVIEW INSTRUCTIONS (CRITICAL)

当你收到 `### PREVIOUS REVIEW REPORT` 时，必须执行以下步骤：

1. **检查上一轮问题状态**：
   - 仔细阅读 PREVIOUS REVIEW REPORT 中列出的所有问题
   - 对比当前 CODE DIFF，判断每个问题是否已修复

2. **已修复问题的处理规则（重要）**：
   - 如果某个问题在当前 CODE DIFF 中已经修复（代码已按建议修改或问题代码已删除），**不要**在 `findings` 或 `risks` 中重复报告该问题
   - 如果问题已修复，你可以在报告开头简要说明："上一轮报告中的问题 X 已修复"，但不要将其作为新的 finding 列出
   - **只报告当前 diff 中仍然存在的问题或新发现的问题**

3. **部分修复或未修复问题的处理**：
   - 如果问题部分修复但仍存在，可以报告剩余部分
   - 如果问题完全未修复，可以重新报告，但应注明这是上一轮已提出的问题

4. **避免重复报告**：
   - 不要因为 previous report 中提到了某个问题，就在当前 findings 中重复报告
   - 只有当问题在当前 diff 中仍然存在时，才应该报告
   - 如果问题已修复，Summary Agent 会在增量追踪中标记为 [FIXED]，你不需要重复报告

**核心原则**：Previous review 是用来帮助你了解历史上下文，而不是让你重复报告已修复的问题。专注于当前 diff 中的新问题或仍未解决的问题。
"""
