"""
SummaryOutput：Summary Agent LLM 输出的严格 Pydantic 校验模型与 JSON Schema 定义。

与 ReviewResult 的区别：
- 仅覆盖 LLM 必须直接输出的 3 个顶级字段（llm_result / line_comments / issues）
- 所有字段均为**必填**（无默认值），保证 LLM 不省略任何字段
- 用于 validate_json skill 在 ReviewResult 补充运行时字段前的前置严格校验
- SUMMARY_JSON_SCHEMA 用于 LiteLLM response_format structured outputs 强制输出合规 JSON
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


Severity = Literal["critical", "major", "minor"]


# ── Pydantic 模型（extra="allow"：允许 LLM 附带额外诊断字段，但不放宽已知字段校验）──

class SummaryLineComment(BaseModel):
    model_config = ConfigDict(extra="allow")

    new_path: str = Field(min_length=1)
    body: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class SummaryLineComments(BaseModel):
    model_config = ConfigDict(extra="allow")

    comments: list[SummaryLineComment]


class SummaryIssueLocation(BaseModel):
    model_config = ConfigDict(extra="allow")

    path: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)


class SummaryIssue(BaseModel):
    model_config = ConfigDict(extra="allow")

    severity: Severity
    title: str = Field(min_length=1)
    count: int = Field(ge=1)
    locations: list[SummaryIssueLocation]


class SummaryOutput(BaseModel):
    """LLM 直接输出的结构化报告，包含三个必须存在的顶级字段。"""

    model_config = ConfigDict(extra="allow")

    llm_result: str = Field(min_length=1)
    line_comments: SummaryLineComments
    issues: list[SummaryIssue]


# ── JSON Schema（由 SummaryOutput 自动生成，与 Pydantic 模型保持单一真相来源）──────
# 用于 sdk_runtime.query_subagent_structured() 的 response_format 参数。
SUMMARY_JSON_SCHEMA: dict[str, Any] = {
    "name": "summary_output",
    "schema": SummaryOutput.model_json_schema(),
}


def validate_summary_output(report: dict[str, Any]) -> list[str]:
    """
    对 LLM 原始输出进行严格字段校验。

    返回错误消息列表；列表为空表示校验通过。
    不修改 report，不抛出异常。
    """
    try:
        SummaryOutput.model_validate(report)
        return []
    except ValidationError as exc:
        errors: list[str] = []
        for item in exc.errors():
            loc = ".".join(str(part) for part in item.get("loc", ())) or "<root>"
            msg = str(item.get("msg", "invalid"))
            errors.append(f"{loc}: {msg}")
        return errors
