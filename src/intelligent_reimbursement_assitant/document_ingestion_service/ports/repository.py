from contextlib import AbstractContextManager
from typing import Protocol, TypedDict
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue
from sqlalchemy.orm import Session

from intelligent_reimbursement_assitant.contracts.models import ArtifactRef, SourceReference
from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    AcceptedUpload,
    Document,
    DocumentRegistration,
    DocumentView,
    IngestionAccepted,
    KnowledgeBase,
    KnowledgeSnapshot,
    PolicyDate,
    PolicyMetadata,
    SnapshotEntry,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionKind,
    ExecutionStatus,
    IngestionJobView,
    IngestionStage,
)


class PublishedSourceArtifacts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_ref: ArtifactRef
    parsed_document_ref: ArtifactRef
    chunk_batch_ref: ArtifactRef


class KnowledgeBaseItem(TypedDict):
    kb_id: UUID
    name: str


class DocumentItem(TypedDict):
    document_id: UUID
    title: str
    issuer: str


class IngestionSource(TypedDict):
    document_id: UUID
    source_ref: ArtifactRef
    stage: IngestionStage
    checkpoint: dict[str, JsonValue]


class CompletionFacts(TypedDict):
    status: ExecutionStatus
    stage: IngestionStage
    checkpoint: dict[str, JsonValue]
    document_id: UUID


class DocumentRepository(Protocol):
    def session(self) -> AbstractContextManager[Session]: ...
    def create_knowledge_base(self, session: Session, value: KnowledgeBase) -> None: ...
    def find_job_identity(
        self, session: Session, job_id: UUID
    ) -> tuple[ExecutionKind, SourceReference | None] | None: ...
    def lock_source(self, session: Session, source: SourceReference) -> bool: ...
    def load_source_parsed_ref(
        self, session: Session, source: SourceReference, generation_id: UUID | None
    ) -> ArtifactRef | None: ...
    def has_active_ingestion(self, session: Session, source: SourceReference) -> bool: ...
    def get_source_file(
        self, session: Session, source: SourceReference
    ) -> tuple[str, ArtifactRef] | None: ...
    def get_document_view(self, session: Session, document_id: UUID) -> DocumentView | None: ...
    def read_event_head(
        self, session: Session, job_id: UUID
    ) -> tuple[IngestionJobView, int] | None: ...
    def load_knowledge_snapshot(
        self, session: Session, kb_id: UUID
    ) -> KnowledgeSnapshot | None: ...
    def load_published_artifacts(
        self, session: Session, snapshot: KnowledgeSnapshot
    ) -> dict[UUID, PublishedSourceArtifacts]: ...
    def publication_scope(self, document_id: UUID) -> AbstractContextManager[Session | None]:
        """Try the session lock on a dedicated connection; None means another publisher owns it."""
        ...

    def load_publication_sources(
        self, session: Session, source: SourceReference
    ) -> tuple[Document, PolicyMetadata, list[SnapshotEntry]]: ...

    def update_policy_metadata(
        self, session: Session, source: SourceReference, metadata: PolicyMetadata
    ) -> None: ...

    def publish_source(
        self, session: Session, source: SourceReference, generation_id: UUID
    ) -> None: ...

    def increment_knowledge_revision(self, session: Session, kb_id: UUID) -> None: ...

    def load_completion_facts(self, session: Session, job_id: UUID) -> CompletionFacts: ...
    def knowledge_base_exists(self, session: Session, kb_id: UUID) -> bool: ...
    def get_document(self, session: Session, document_id: UUID) -> Document | None: ...
    def find_document(
        self, session: Session, registration: DocumentRegistration
    ) -> Document | None: ...
    def find_latest_ingestion_accepted(
        self, session: Session, document_id: UUID, source_hash: str
    ) -> IngestionAccepted | None: ...
    def get_or_create_document(
        self, session: Session, registration: DocumentRegistration
    ) -> Document: ...
    def insert_source_if_absent(
        self, session: Session, document_id: UUID, upload: AcceptedUpload, policy: PolicyMetadata
    ) -> bool: ...
    def create_ingestion_job(
        self,
        session: Session,
        job_id: UUID,
        document_id: UUID,
        source_hash: str,
        *,
        checkpoint: dict[str, JsonValue] | None = None,
        stage: IngestionStage = IngestionStage.REGISTERED,
    ) -> None: ...
    def find_source_key(
        self, session: Session, document_id: UUID, source_hash: str
    ) -> str | None: ...
    def referenced_artifact_keys(self, session: Session) -> set[str]: ...
    def list_knowledge_bases(self, session: Session) -> list[KnowledgeBaseItem]: ...
    def list_documents(
        self, session: Session, kb_id: UUID, issuer: str | None, title: str | None
    ) -> list[DocumentItem]: ...
    def get_ingestion_job(self, session: Session, job_id: UUID) -> IngestionJobView | None: ...

    def load_ingestion_source(self, session: Session, job_id: UUID) -> IngestionSource: ...

    def save_parsed_checkpoint(
        self,
        session: Session,
        job_id: UUID,
        expected_stage: IngestionStage,
        target_stage: IngestionStage,
        parsed_ref: ArtifactRef | None,
        extracted: PolicyDate | None = None,
    ) -> None: ...

    def get_or_create_chunking_generation(
        self, session: Session, job_id: UUID, strategy: str
    ) -> tuple[UUID, str]: ...

    def mark_chunking_model_started(
        self, session: Session, job_id: UUID, started: bool
    ) -> None: ...

    def save_chunked_checkpoint(
        self, session: Session, job_id: UUID, chunk_ref: ArtifactRef
    ) -> None: ...

    def get_or_bind_index_profile(self, session: Session, job_id: UUID) -> str: ...

    def load_indexing_source_metadata(
        self, session: Session, job_id: UUID
    ) -> tuple[Document, PolicyMetadata]: ...

    def advance_ingestion_stage(
        self,
        session: Session,
        job_id: UUID,
        expected_stage: IngestionStage,
        target_stage: IngestionStage,
    ) -> None: ...
