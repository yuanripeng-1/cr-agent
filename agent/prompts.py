COMMON_CONSTRAINTS_EN = """
- Focus ONLY on new code added in the PR diff (lines starting with '+').
- You only see changed code hunks (diff hunks), not the entire codebase. Do NOT assume missing context.
- Some added lines in `CODE DIFF` may include an injected line-number prefix for grounding, formatted like `+[106]\tactual code` or `+[~106]\tactual code`.
- Treat the `[106]` / `[~106]` segment as metadata only, NOT as part of the real diff content, code, syntax, identifier, string, or business logic.
- When reasoning about code semantics, mentally strip the injected line-number prefix and read `+[106]\treturn x` as `+return x`.
- When outputting `start_line` / `end_line`, use the annotated new-file line number from the injected prefix if present. `~` means the line number is approximate, so only use it when the issue location is still clear from the diff.
- Output MUST be valid YAML and NOTHING ELSE.
- Every multi-line string field MUST use a block scalar with '|' and proper indentation.
- All natural language content (descriptions, analysis, rationale, comments) MUST be in English unless the role prompt explicitly overrides it.
- Only report high-confidence issues: every reported issue must include `confidence` (0-100) and must be >= 85; otherwise do not output it.
- If no issue with confidence >= 85 is found, the relevant list fields MUST be empty arrays `[]`. Do not fabricate low-value suggestions just to fill the schema.
- Do not review or comment on version-only changes (for example dependency version bumps or version string updates).
"""

SUMMARY_LANGUAGE_CONSTRAINT = """
- 所有自然语言内容（描述、分析、rationale、comments）必须使用简体中文。
"""

DIFF_FORMAT_NOTE = """
You will receive a PR diff with '+', '-', and ' ' lines. Base findings on '+' lines.
Some '+' lines may be annotated as `+[123]\t...` or `+[~123]\t...`; the bracketed part is injected line-number metadata, not code.
"""

import os

def load_prompt(name: str) -> str:
    """
    Load role prompt markdown and prepend common constraints to improve compliance.
    """
    path = os.path.join("prompt", f"{name}.md")
    base_constraints = COMMON_CONSTRAINTS_EN
    if name == "summary":
        base_constraints = f"{COMMON_CONSTRAINTS_EN}\n{SUMMARY_LANGUAGE_CONSTRAINT}"
    if os.path.exists(path):
        with open(path, "r") as f:
            role_prompt = f.read()
        return f"{base_constraints}\n{DIFF_FORMAT_NOTE}\n\n{role_prompt}"
    return f"{base_constraints}\n{DIFF_FORMAT_NOTE}\n\nPrompt {name} not found."

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

When you receive `### PREVIOUS REVIEW REPORT`, you must follow these rules:

1. **Check the status of previously reported issues**:
   - Read all issues listed in `PREVIOUS REVIEW REPORT` carefully.
   - Compare them with the current `CODE DIFF` and determine whether each issue has been fixed.

2. **Rules for fixed issues (important)**:
   - If an issue has already been fixed in the current `CODE DIFF` (the code was updated as suggested or the problematic code was removed), **do not** report it again in `findings` or `risks`.
   - If useful, you may briefly mention that a previous issue has been fixed, but do not list it as a new finding.
   - **Only report issues that still exist in the current diff or newly introduced issues.**

3. **Partially fixed or unresolved issues**:
   - If an issue is only partially fixed and still exists, report the remaining problem.
   - If an issue is completely unresolved, you may report it again, but make it clear that it was already raised in the previous review.

4. **Avoid duplicate reporting**:
   - Do not repeat an issue in current `findings` just because it appeared in the previous report.
   - Report it only if it still exists in the current diff.
   - If the issue is fixed, the Summary Agent will mark it as `[FIXED]`; you do not need to report it again.

**Core principle**: The previous review is historical context, not a checklist for repeating already-fixed issues. Focus on newly introduced issues and issues that still remain in the current diff.
"""
