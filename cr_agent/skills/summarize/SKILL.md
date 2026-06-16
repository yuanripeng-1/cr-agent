# summarize_report

## Purpose
Aggregate collected context and dimension expert reports into the final code review content.

## When To Use
Call this skill after `dimension_review`. If validation fails, call it again with validation errors.

## Inputs
- collected context
- dimension expert reports and normalized findings
- validation errors from the previous attempt, if any
- `prompt/summary.md`
- `prompt/rules/summaryRule.md`

## Tools
The summary subagent must not use tools.

## Output
Return JSON content fields:
- `llm_result`
- `line_comments`
- `issues`

Runtime fields such as `status`, `log_path`, and `tokens_consume` are added by the orchestrator.

## Retry Contract
When `validation_errors` is non-empty, fix the prior output shape and do not expand review scope.

## Failure Policy
Empty or invalid output is handled by validation and main-agent retry.
