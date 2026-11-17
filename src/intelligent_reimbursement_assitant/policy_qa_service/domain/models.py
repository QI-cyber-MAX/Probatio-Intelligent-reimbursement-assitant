"""Persisted question inputs and the four business message payloads."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from intelligent_reimbursement_assitant.contracts.models import ArtifactRef, SourceLocator
from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    Chunk,
    KnowledgeSnapshot,
    SnapshotEntry,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import ExecutionStatus


class QAInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["question"] = "question"
    question: str
    context_message_ids: list[UUID]
    snapshot: KnowledgeSnapshot
    wiki_ref: ArtifactRef | None = None


class UserFact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fact_id: str
    meaning: str
    kind: Literal["text", "number", "date", "boolean"]
    unit: str | None
    source_message_id: UUID


class NumberFactValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["number"] = "number"
    value: Decimal = Field(allow_inf_nan=False)


class DateFactValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["date"] = "date"
    date_from: date
    date_to: date

    @model_validator(mode="after")
    def ordered_interval(self) -> DateFactValue:
        if self.date_from >= self.date_to:
            raise ValueError("date_from must precede date_to")
        return self


type FactValue = Annotated[NumberFactValue | DateFactValue, Field(discriminator="kind")]


type Coverage = Literal["COMPLETE", "PARTIAL", "UNSUPPORTED"]


class SupportedClaim(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    claim_id: str
    text: str
    fact_ids: list[str]
    evidence_chunk_ids: list[str]


class EvidenceGap(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["USER_FACT", "POLICY_MISSING", "CONFLICT"]
    description: str
    evidence_chunk_ids: list[str]


class Citation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    number: int
    chunk_id: str
    source_text: str
    locators: list[SourceLocator]


class AnswerItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: str
    question: str
    coverage: Coverage
    text: str
    claims: list[SupportedClaim]
    gaps: list[EvidenceGap]
    citation_numbers: list[int]


class AnswerResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["answer"] = "answer"
    items: list[AnswerItem]
    facts: list[UserFact]
    citations: list[Citation]


class ClarificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_ids: list[str]
    question: str


class TaskItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: str
    question: str
    kind: Literal["general_rule", "personal_application"]
    date_from: date | None
    date_to: date | None
    fact_ids: list[str]


class TaskPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    facts: list[UserFact]
    items: list[TaskItem]
    clarification: ClarificationRequest | None


class ClarificationMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["clarification_request"] = "clarification_request"
    interrupt_id: str
    request: ClarificationRequest


class ClarificationReply(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["clarification_reply"] = "clarification_reply"
    text: str


type MessagePayload = Annotated[
    QAInput | AnswerResult | ClarificationMessage | ClarificationReply, Field(discriminator="kind")
]


class QAMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: UUID
    conversation_id: UUID
    reply_to_message_id: UUID | None
    payload: MessagePayload
    created_at: AwareDatetime


class QAJobView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID
    status: ExecutionStatus
    retry_count: int
    max_retries: int
    next_retry_at: AwareDatetime | None
    last_error_code: str | None
    terminal_reason: str | None


class PublicQuestion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["question"] = "question"
    question: str
    job_id: UUID


type PublicMessagePayload = Annotated[
    PublicQuestion | AnswerResult | ClarificationMessage | ClarificationReply,
    Field(discriminator="kind"),
]


class PublicQAMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: UUID
    conversation_id: UUID
    reply_to_message_id: UUID | None
    payload: PublicMessagePayload
    created_at: AwareDatetime


class ConversationHistoryPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    conversation_id: UUID
    active_job_id: UUID | None
    messages: list[PublicQAMessage]
    next_before: UUID | None


class AgentTurn(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    qa_run_id: UUID
    conversation_id: UUID
    message_id: UUID
    input_ref: str
    answer_ref: str | None


class SearchQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query_id: str
    item_id: str
    mode: Literal["keyword", "semantic"]
    text: str
    document_ids: list[UUID]
    retrieval_round: int


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: SearchQuery
    entries: list[SnapshotEntry]
    limit: int = Field(strict=True, gt=0)


class SearchDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["search"]
    query_id: str


class ReadDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["read"]
    item_id: str
    chunk_ids: list[str]


class WikiReadDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action: Literal["wiki"]
    item_id: str
    page_id: str


class AssessDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["assess"]


class SearchBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query_id: str
    mode: Literal["keyword", "semantic"]
    index_profile_id: str
    generation_ids: list[UUID]
    chunks: list[Chunk]


class ItemAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: str
    coverage: Coverage
    claims: list[SupportedClaim]
    gaps: list[EvidenceGap]


class EvidenceCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    retrieval_note: str | None = None

    item: TaskItem
    facts: list[UserFact]
    entries: list[SnapshotEntry]
    chunks: list[Chunk]


type EvidenceAssessment = list[ItemAssessment]


class AnswerSection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item_id: str
    text: str
    claim_ids: list[str]


type AnswerSections = list[AnswerSection]
