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

    # 模型请求消耗的输入 token 数。
    input_tokens: int = Field(default=0, ge=0)
    # 模型响应消耗的输出 token 数。
    output_tokens: int = Field(default=0, ge=0)
    # 本次审查估算或回传的模型调用成本。
    cost: float = Field(default=0.0, ge=0)


class LineComment(BaseModel):
    # GitLab 风格的行级评论契约。后端需要这些字段把评论挂到
    # 变更文件的指定行范围上。
    model_config = ConfigDict(extra="allow")

    # 被评论的新文件路径,对应 GitLab diff position 的 new_path。
    new_path: str = Field(min_length=1)
    # 行级评论正文,用于回写到代码托管平台。
    body: str = Field(min_length=1)
    # 评论覆盖范围的起始行号。
    start_line: int = Field(ge=1)
    # 评论覆盖范围的结束行号。
    end_line: int = Field(ge=1)

    @field_validator("end_line")
    @classmethod
    def end_line_must_be_valid(cls, value: int, info) -> int:
        # 在后端回写 GitLab 评论前提前拦截非法行号范围。
        return _validate_line_range_end(value, info)


class LineComments(BaseModel):
    model_config = ConfigDict(extra="allow")

    # 本次审查生成的行级评论列表。
    comments: list[LineComment] = Field(default_factory=list)


class IssueLocation(BaseModel):
    # issue 位置结构有意和行级评论保持接近,但它属于报告摘要元信息,
    # 不一定会被后端转换成 GitLab 行级评论。
    model_config = ConfigDict(extra="allow")

    # 问题所在文件路径。
    path: str = Field(min_length=1)
    # 问题所在范围的起始行号。
    start_line: int = Field(ge=1)
    # 问题所在范围的结束行号。
    end_line: int = Field(ge=1)

    @field_validator("end_line")
    @classmethod
    def end_line_must_be_valid(cls, value: int, info) -> int:
        return _validate_line_range_end(value, info)


class IssueSummary(BaseModel):
    model_config = ConfigDict(extra="allow")

    # 问题严重级别,用于报告排序和风险呈现。
    severity: Severity
    # 问题标题,用于摘要列表展示。
    title: str = Field(min_length=1)
    # 同类问题出现次数。
    count: int = Field(ge=1)
    # 同类问题关联的代码位置列表。
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
    # 展示给用户的 Markdown 审查报告。
    llm_result: str = Field(min_length=1)
    # 本次审查运行状态,用于后端判断结果是否可消费。
    status: RunStatus
    # run.log 的路径,用于排查审查过程问题。
    log_path: str = ""
    # 本次审查的 token 和成本消耗信息。
    tokens_consume: TokenUsage = Field(default_factory=TokenUsage)
    # 可回写到代码托管平台的行级评论集合。
    line_comments: LineComments = Field(default_factory=LineComments)
    # 报告摘要中的结构化问题列表。
    issues: list[IssueSummary] = Field(default_factory=list)


def load_review_result(result_path: Path) -> ReviewResult:
    return ReviewResult.model_validate_json(result_path.read_text(encoding="utf-8"))


def write_review_result(result_path: Path, result: ReviewResult) -> None:
    result_path.write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
