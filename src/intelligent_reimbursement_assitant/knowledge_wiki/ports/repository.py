from contextlib import AbstractContextManager
from datetime import datetime
from typing import Protocol, TypedDict
from uuid import UUID

from pydantic import JsonValue
from sqlalchemy.orm import Session

from intelligent_reimbursement_assitant.contracts.models import ArtifactRef
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionContext,
    ExecutionStatus,
)
from intelligent_reimbursement_assitant.knowledge_wiki.domain.models import WikiBuildInput


class WikiBuild(TypedDict):
    job_id: UUID
    kb_id: UUID
    input: WikiBuildInput
    checkpoint: dict[str, JsonValue]
    status: ExecutionStatus
    next_retry_at: datetime | None
    last_error_code: str | None
    terminal_reason: str | None


class WikiRepository(Protocol):
    def session(self) -> AbstractContextManager[Session]: ...
    def list_update_candidates(
        self, session: Session, after: UUID | None, limit: int
    ) -> list[UUID]: ...
    def lock_knowledge_base(self, session: Session, kb_id: UUID) -> tuple[str, int]: ...
    def find_active_build(self, session: Session, kb_id: UUID) -> WikiBuild | None: ...
    def load_latest_build(
        self, session: Session, kb_id: UUID, *, successful: bool = False, automatic: bool = False
    ) -> WikiBuild | None: ...
    def find_build(self, session: Session, job_id: UUID) -> WikiBuild | None: ...
    def job_id_in_use(self, session: Session, job_id: UUID) -> bool: ...
    def create_build(self, session: Session, job_id: UUID, value: WikiBuildInput) -> None: ...
    def load_build(self, session: Session, job_id: UUID) -> WikiBuild: ...
    def save_checkpoint(
        self, session: Session, ctx: ExecutionContext, checkpoint: dict[str, JsonValue]
    ) -> None: ...
    def load_published_build(self, session: Session, kb_id: UUID) -> WikiBuild | None: ...
    def find_published_ref(
        self, session: Session, kb_id: UUID, revision: int
    ) -> ArtifactRef | None: ...
    def set_published_build(self, session: Session, kb_id: UUID, job_id: UUID) -> None: ...
