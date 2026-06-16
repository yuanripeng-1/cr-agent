# validate_json

## Purpose
Validate that the final review result can be consumed by downstream systems.

## When To Use
Call immediately after every `summarize_report` attempt.

## Agent Policy
This skill is deterministic Python code. It must not call an agent and must not use tools.

## Validation Scope
- `ReviewResult` schema
- `line_comments.comments` and `issues.locations` alignment
- relative file paths
- positive line ranges
- diff added-line range when diff context is available

## Output
Return `ValidationResult(valid, errors)`.

## Failure Policy
Never mutate the report. Return validation errors for the main agent to decide whether to retry summary.
