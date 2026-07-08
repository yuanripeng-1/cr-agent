from __future__ import annotations

import json
from pathlib import Path

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator, model_validator

from cr_agent.core.agent_config import Platform


class ReviewInput(BaseModel):
    """
    context.json 的外部输入契约,对应架构里的“最初准备”输入。

    这里只约束后端传给 agent 的输入边界。
    skill 之间的中间结构暂时不在这里定义,等对应 skill 开发前再冻结。
    """

    # 保留后端传来的未知字段,避免 GitLab/infcode 后续增加元信息时,
    # agent 层模型必须立刻跟着改。
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    @model_validator(mode="before")
    @classmethod
    def normalize_requirement_aliases(cls, raw_value: object) -> object:
        if not isinstance(raw_value, dict):
            return raw_value

        payload = dict(raw_value)
        if "requirements_Doc" in payload and "requirements_doc" in payload:
            # requirements_Doc 是历史后端字段,优先级更高。删除 snake_case 副本,
            # 避免 extra="allow" 把它保存为额外字段并在 model_dump() 时覆盖正式字段。
            payload.pop("requirements_doc")
        return payload

    # code review 的核心输入。缺少这些字段时,agent 无法识别任务、
    # 理解开发意图、定位代码仓库或审查 diff。
    # code review 任务唯一标识,用于关联工作区和输出目录。
    task_id: str = Field(min_length=1)
    # code review 任务标题,用于报告展示和模型理解改动主题。
    title: str = Field(min_length=1)
    # 待审查的 diff 文本,是各维度审查的核心代码输入。
    diff_content: str = Field(min_length=1)
    # 被审查项目的根目录,用于定位仓库文件和补充上下文。
    project_root: str = Field(min_length=1)
    # 本次变更关联的 commit message 列表,用于补充开发意图。
    commit_messages: list[str] = Field(default_factory=list)

    # GitLab/MR 元信息用于行级评论和追踪,但当前真实样例不保证
    # 每个字段都有值,因此这里按兼容字段处理。
    # GitLab 项目 ID,用于后续回写 MR 评论。
    project_id: int | None = None
    # GitLab merge request IID,用于定位具体 MR。
    mr_iid: int | None = None
    # MR 描述文本,用于补充需求背景和变更说明。
    description: str = ""
    # MR diff 的 base commit SHA,用于定位评论基线。
    base_sha: str = ""
    # MR diff 的 head commit SHA,用于定位评论目标版本。
    head_sha: str = ""
    # MR diff 的 start commit SHA,用于兼容 GitLab position 信息。
    start_sha: str = ""
    # MR 源分支名称,用于识别变更来源。
    source_branch: str = ""
    # MR 目标分支名称,用于识别合入目标。
    target_branch: str = ""
    # 后端生成的 diff 文件路径,用于必要时回读原始 diff。
    diff_file_path: str = ""
    # 上一次审查报告内容,用于增量审查时参考历史结论。
    previous_report: str = ""

    # per-task git token,用于 git 远程操作鉴权。优先级高于 agent_config [git].token。
    # 这是后端新增兼容字段;日志必须脱敏,只记 configured/短 hash。
    git_token: str = ""

    # 历史后端字段名是 requirements_Doc。这里同时接受 requirements_doc,
    # 方便 Python 内部调用使用 snake_case,又不破坏外部 JSON 契约。
    # 外部需求文档内容或路径,用于把业务需求纳入审查上下文。
    requirements_doc: str = Field(
        default="",
        validation_alias=AliasChoices("requirements_Doc", "requirements_doc"),
    )

    # context.platform 只作为上下文信息。真正决定工具集切换的平台来源仍然是
    # RUN.sh --platform 或 agent_config.toml。
    # 后端传入的平台上下文,用于记录调用来源但不覆盖启动配置。
    platform: Platform | None = None

    @field_validator("commit_messages", mode="before")
    @classmethod
    def normalize_commit_messages(cls, raw_value: object) -> list[str]:
        # 兼容旧调用方把单条 commit message 作为字符串传入的情况。
        # 统一成 list 后,后续流水线就不需要再分支判断。
        return _normalize_commit_messages(raw_value)


def _normalize_commit_messages(raw_value: object) -> list[str]:
    if raw_value is None:
        return []
    if isinstance(raw_value, list):
        return [str(item).strip() for item in raw_value if str(item).strip()]
    if isinstance(raw_value, str):
        stripped = raw_value.strip()
        return [stripped] if stripped else []
    return []


def load_review_input(context_path: Path) -> ReviewInput:
    payload = json.loads(context_path.read_text(encoding="utf-8"))
    return ReviewInput.model_validate(payload)
