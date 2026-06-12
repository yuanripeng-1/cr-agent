from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


Severity = Literal["critical", "major", "minor", "info"]
RunStatus = Literal["success", "failed", "error", "bootstrap_ready"]


def _validate_line_range_end(value: int, info) -> int:
    start_line = info.data.get("start_line")
    if start_line is not None and value < start_line:
        raise ValueError("end_line must be greater than or equal to start_line")
    return value


class TokenUsage(BaseModel):
    # 保留 provider 专属 token/cost 细分字段的扩展空间,
    # 同时固定后端当前已经消费的三个基础字段。
    model_config = ConfigDict(extra="allow")

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    cost: float = Field(default=0.0, ge=0)


class LineComment(BaseModel):
    # GitLab 风格的行级评论契约。后端需要这些字段把评论挂到
    # 变更文件的指定行范围上。
    model_config = ConfigDict(extra="allow")

    new_path: str = Field(min_length=1)
    body: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @field_validator("end_line")
    @classmethod
    def end_line_must_be_valid(cls, value: int, info) -> int:
        # 在后端回写 GitLab 评论前提前拦截非法行号范围。
        return _validate_line_range_end(value, info)


class LineComments(BaseModel):
    model_config = ConfigDict(extra="allow")

    comments: list[LineComment] = Field(default_factory=list)


class IssueLocation(BaseModel):
    # issue 位置结构有意和行级评论保持接近,但它属于报告摘要元信息,
    # 不一定会被后端转换成 GitLab 行级评论。
    model_config = ConfigDict(extra="allow")

    path: str = Field(min_length=1)
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    @field_validator("end_line")
    @classmethod
    def end_line_must_be_valid(cls, value: int, info) -> int:
        return _validate_line_range_end(value, info)


class IssueSummary(BaseModel):
    model_config = ConfigDict(extra="allow")

    severity: Severity
    title: str = Field(min_length=1)
    count: int = Field(ge=1)
    locations: list[IssueLocation] = Field(default_factory=list)


class ReviewResult(BaseModel):
    """
    后端消费的最终 result.json 契约。

    允许额外字段承载未来产品元信息,但现有后端消费字段必须稳定,
    这样字段回归能尽早在契约校验阶段暴露。
    """

    # 顶层模型保持宽松:产品代码可增加 task_id/platform 或未来 telemetry 字段,
    # 下面这些冻结字段才是当前后端兼容边界。
    model_config = ConfigDict(extra="allow")

    # llm_result 是展示给用户的 Markdown 报告;line_comments/issues 是
    # 下游系统用于回写评论和展示摘要的结构化视图。
    llm_result: str = Field(min_length=1)
    status: RunStatus
    log_path: str = ""
    tokens_consume: TokenUsage = Field(default_factory=TokenUsage)
    line_comments: LineComments = Field(default_factory=LineComments)
    issues: list[IssueSummary] = Field(default_factory=list)


def load_review_result(result_path: Path) -> ReviewResult:
    return ReviewResult.model_validate_json(result_path.read_text(encoding="utf-8"))


def write_review_result(result_path: Path, result: ReviewResult) -> None:
    result_path.write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
