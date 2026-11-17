"""Shared execution vocabulary and the persisted Outbox contract."""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ExecutionKind(StrEnum):
    INGESTION = "INGESTION"
    AGENT_TURN = "AGENT_TURN"
    WIKI_BUILD = "WIKI_BUILD"


class ExecutionStatus(StrEnum):
    ACCEPTED = "ACCEPTED"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRY_WAIT = "RETRY_WAIT"
    WAITING_INPUT = "WAITING_INPUT"
    CANCELLING = "CANCELLING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class QueueReason(StrEnum):
    INITIAL = "INITIAL"
    AUTO_RETRY = "AUTO_RETRY"
    INPUT_RESUME = "INPUT_RESUME"


class IngestionStage(StrEnum):
    REGISTERED = "REGISTERED"
    PARSING = "PARSING"
    NORMALIZING = "NORMALIZING"
    CHUNKING = "CHUNKING"
    EMBEDDING = "EMBEDDING"
    INDEXING = "INDEXING"
    PUBLISHING = "PUBLISHING"


class ExecutionSettings(BaseModel):
    """Server configuration; all three budget amounts use tokens as their unit."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_retries: int = Field(default=3, ge=0, strict=True)
    budget_limit: Decimal = Field(ge=0, allow_inf_nan=False)


class JobEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    kind: ExecutionKind


class QueueDelivery(JobEnvelope):
    entry_id: str


class ExecutionContext(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    lease_epoch: int


class Claimed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal["CLAIMED"] = "CLAIMED"
    context: ExecutionContext


class ClaimSkipped(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal["SKIP"] = "SKIP"
    reason: str


class ClaimDeferred(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal["DEFER"] = "DEFER"
    reason: str


type ClaimResult = Claimed | ClaimSkipped | ClaimDeferred


class WorkflowSucceeded(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal["SUCCEEDED"] = "SUCCEEDED"


class WorkflowWaiting(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    outcome: Literal["WAITING_INPUT"] = "WAITING_INPUT"
    waiting_ref: str


type WorkflowResult = WorkflowSucceeded | WorkflowWaiting


class OutboxEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: UUID
    job_id: UUID
    kind: ExecutionKind
    created_at: datetime
    published_at: datetime | None


class IngestionJobView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    status: ExecutionStatus
    stage: IngestionStage
    retry_count: int
    max_retries: int
    next_retry_at: datetime | None
    last_error_code: str | None
    terminal_reason: str | None
