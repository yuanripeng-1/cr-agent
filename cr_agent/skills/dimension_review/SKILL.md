# dimension_review

## Purpose
Run configured review dimensions and produce expert reports for the summary stage.

## When To Use
Call this skill after `collect_context` has completed.

## Inputs
- collected context
- `config/dimensions.toml`
- `prompt/<dimension>.md`
- `prompt/rules/<dimension>Rule.md`

## Tools
Each dimension subagent may only use tools provided by `ToolFacade.tools_for("dimension")`.

## Execution Contract
- Select dimensions by platform.
- Use `defaults.concurrency` and `asyncio.Semaphore` for concurrency control.
- Each dimension must run in isolation with its own prompt and SDK options.
- Each dimension writes `dimensions/<dimension>.json`.
- The skill writes `dimensions/manifest.json` after all dimensions finish.

## Subagent Output
The dimension subagent must return YAML expert report text. Do not wrap it in Markdown fences.

## Adapter Responsibilities
`skill.py` must parse the YAML, preserve `raw_yaml`, normalize findings, and write JSON artifacts.

## Failure Policy
One failed dimension must not fail the whole task. If all dimensions fail, raise an error.
