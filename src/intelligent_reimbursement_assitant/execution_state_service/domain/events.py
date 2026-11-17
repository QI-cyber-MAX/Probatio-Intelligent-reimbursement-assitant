"""Public task events, independent of business messages and workflow checkpoints."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionContext,
    ExecutionStatus,
)


class ProgressStage(StrEnum):
    PARSING = "PARSING"
    NORMALIZING = "NORMALIZING"
    CHUNKING = "CHUNKING"
    EMBEDDING = "EMBEDDING"
    INDEXING = "INDEXING"
    PUBLISHING = "PUBLISHING"
    UNDERSTANDING = "UNDERSTANDING"
    APPLYING_CLARIFICATION = "APPLYING_CLARIFICATION"
    SELECTING_TOOLS = "SELECTING_TOOLS"
    KEYWORD_SEARCH = "KEYWORD_SEARCH"
    QUERY_EMBEDDING = "QUERY_EMBEDDING"
    SEMANTIC_SEARCH = "SEMANTIC_SEARCH"
    RERANKING = "RERANKING"
    CHUNK_READ = "CHUNK_READ"
    ASSESSING_EVIDENCE = "ASSESSING_EVIDENCE"
    PREPARING_CLARIFICATION = "PREPARING_CLARIFICATION"
    GENERATING_ANSWER = "GENERATING_ANSWER"
    DRAFTING_ANSWER = "DRAFTING_ANSWER"
    REVIEWING_ANSWER = "REVIEWING_ANSWER"
    PUBLISHING_ANSWER = "PUBLISHING_ANSWER"


PROGRESS_TEXT = dict(
    zip(
        ProgressStage,
        (
            "正在解析文档",
            "正在归一化结构",
            "正在分块",
            "正在生成文档向量",
            "正在写入索引",
            "正在发布索引代次",
            "正在理解问题",
            "正在处理补充信息",
            "正在选择检索工具",
            "正在调用关键词检索",
            "正在生成查询向量",
            "正在调用语义检索",
            "正在重排检索结果",
            "正在读取证据正文",
            "正在判断证据",
            "正在准备澄清问题",
            "正在生成回答",
            "正在组织回答",
            "正在检查回答",
            "正在发布回答",
        ),
        strict=True,
    )
)

type ReportProgress = Callable[[ProgressStage], Awaitable[None]]
type ProgressFactory = Callable[[ExecutionContext, str | None, str | None], ReportProgress]


class JobStatusEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: ExecutionStatus
    next_retry_at: datetime | None
    last_error_code: str | None
    terminal_reason: str | None


class JobProgress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: ProgressStage
    item_id: str | None = None
    tool_call_id: str | None = None
    text: str


class PublishedTextDelta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: UUID
    item_id: str | None
    offset: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=512)


class PublishedMessageEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: UUID


type EventType = Literal["status", "progress", "text_delta", "clarification", "answer"]
type EventPayload = JobStatusEvent | JobProgress | PublishedTextDelta | PublishedMessageEvent


class ExecutionEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    sequence: int
    event_type: EventType
    payload: dict[str, JsonValue]
