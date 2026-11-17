"""Immutable wiki content; source and chunk identities are bound by the compiler."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from intelligent_reimbursement_assitant.contracts.models import ArtifactRef, SourceLocator
from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    KnowledgeSnapshot,
)
from intelligent_reimbursement_assitant.document_ingestion_service.ports.repository import (
    PublishedSourceArtifacts,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import ExecutionStatus
from intelligent_reimbursement_assitant.parser.domain.models import ParseReport

type StatementKind = Literal["summary", "relation", "open_question"]
type Topic = Literal[
    "business_context",
    "expense_category_and_scope",
    "process",
    "role",
    "relationship",
    "open_question",
]
type EvidenceLevel = Literal["direct", "derived", "inferred"]


TOPICS = {
    "business_context": "业务上下文",
    "expense_category_and_scope": "经费类型与适用范围",
    "process": "管理与办理流程",
    "role": "人员类别与关系",
    "relationship": "制度关系",
    "open_question": "制度疑点",
}


class WikiBuildInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    snapshot: KnowledgeSnapshot
    artifacts: dict[UUID, PublishedSourceArtifacts]
    base_ref: ArtifactRef | None
    rebuild_sources: bool = False
    edited_pages: dict[str, WikiPage] | None = None


class WikiChunkRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    generation_id: UUID
    chunk_id: str


class WikiStatement(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    kind: StatementKind
    topic: Topic
    evidence_level: EvidenceLevel
    text: str
    chunk_refs: list[WikiChunkRef]
    related_page_ids: list[str]


class WikiPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    page_id: str
    kind: Literal["overview", "document", "synthesis"]
    title: str
    statements: list[WikiStatement]
    child_page_ids: list[str]


class WikiThemePlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    page_id: str
    title: str
    source_page_ids: list[str]


class WikiPackage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    job_id: UUID
    snapshot: KnowledgeSnapshot
    artifacts: dict[UUID, PublishedSourceArtifacts]
    parse_reports: dict[UUID, ParseReport]
    pages: dict[str, WikiPage]


class WikiSourcePageEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    page_id: str
    statements: list[WikiStatement]

    @field_validator("statements")
    @classmethod
    def navigation(cls, statements: list[WikiStatement]) -> list[WikiStatement]:
        topics = [s.topic for s in statements]
        if topics != [topic for topic in TOPICS if topic in topics]:
            raise ValueError("Topics must be unique and follow the specified order")
        for statement in statements:
            expected = {"relationship": "relation", "open_question": "open_question"}.get(
                statement.topic, "summary"
            )
            if statement.kind != expected or (
                statement.evidence_level == "inferred" and statement.kind != "open_question"
            ):
                raise ValueError("Invalid kind/topic/evidence_level combination")
            if not statement.text.strip():
                raise ValueError("Statement text must be nonblank")
        return statements


class WikiSceneEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    page_id: str | None = None
    scene_ref: str = Field(min_length=1)
    title: str = Field(min_length=1)
    source_page_ids: list[str] = Field(min_length=1)
    statements: list[WikiStatement]

    @field_validator("statements")
    @classmethod
    def navigation(cls, statements: list[WikiStatement]) -> list[WikiStatement]:
        return WikiSourcePageEdit.navigation(statements)

    @field_validator("scene_ref", "title")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Must be nonblank")
        return value


class WikiOverviewContextEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str = Field(min_length=1)
    related_scene_refs: list[str] = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Must be nonblank")
        return value


class WikiScenarioEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    scene_ref: str
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        return WikiOverviewContextEdit.nonblank(value)


class WikiOverviewEdit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    business_context: WikiOverviewContextEdit
    scenarios: list[WikiScenarioEdit]


class WikiVersionView(BaseModel):
    job_id: UUID
    snapshot_revision: int
    pages: dict[str, WikiPage]


class WikiJobSummary(BaseModel):
    job_id: UUID
    status: ExecutionStatus
    operation: Literal["build", "edit"]


class WikiWorkspaceView(BaseModel):
    kb_id: UUID
    current_revision: int
    published_version: WikiVersionView | None
    active_job: WikiJobSummary | None


class WikiJobView(WikiJobSummary):
    kb_id: UUID
    next_retry_at: datetime | None
    last_error_code: str | None
    terminal_reason: str | None
    result_version_job_id: UUID | None
    is_current: bool


class WikiChunkOption(BaseModel):
    generation_id: UUID
    chunk_id: str
    ordinal: int
    body_text: str
    locators: list[SourceLocator]


WikiBuildInput.model_rebuild()
