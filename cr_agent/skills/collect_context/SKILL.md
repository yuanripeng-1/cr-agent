# collect_context

## Purpose
Collect the minimum review context needed before dimension review.

## When To Use
Call this skill first in every code review run.

## Inputs
- `context.json` fields from `RuntimeContext.review_input`
- raw diff content
- project root
- title and description
- commit messages
- requirements document path/content reference when available
- previous report when available

## Tools
The context subagent may only use tools provided by `ToolFacade.tools_for("context")`.

## Execution Contract
- Understand the diff first.
- Extract changed files and added line ranges.
- Use `read_file` and `read_file_range` for concrete evidence when needed.
- Use `grep_text` for references when useful.
- Use `semble_search` for semantic context when available.
- Use `crg_query` only when call graph context is useful and available.
- Tool failures must be reported as warnings and must not stop the review.

## Output
Write `collected_context.json` and return a compact summary containing:
- `task_id`
- `artifact_path`
- `summary`
- `warnings`
- `usage`

## Failure Policy
Runtime failure may fail the skill. Individual tool failures must degrade into warnings.
