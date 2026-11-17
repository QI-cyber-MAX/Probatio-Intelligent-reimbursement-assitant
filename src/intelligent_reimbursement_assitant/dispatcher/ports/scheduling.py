from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionContext,
    ExecutionKind,
    ExecutionStatus,
)


class SchedulingRepository(Protocol):
    def session(self) -> Session: ...
    def list_retry_candidates(self, now: datetime, limit: int) -> list[UUID]: ...
    def list_wakeup_candidates(
        self, now: datetime, stale_after: timedelta, wakeup_interval: timedelta, limit: int
    ) -> list[UUID]: ...
    def list_expired_candidates(
        self, status: ExecutionStatus, now: datetime, limit: int
    ) -> list[tuple[ExecutionContext, ExecutionKind]]: ...


class SchedulingState(Protocol):
    def revoke_expired(
        self, session: Session, ctx: ExecutionContext, status: ExecutionStatus
    ) -> ExecutionContext | None: ...
    def record_failure(
        self,
        session: Session,
        ctx: ExecutionContext,
        error: ExecutionError,
        *,
        recovery: bool = False,
        retry_delay: timedelta = timedelta(seconds=30),
    ) -> None: ...
    def confirm_cancel(
        self, session: Session, ctx: ExecutionContext, *, recovery: bool = False
    ) -> None: ...
    def enqueue(self, session: Session, job_id: UUID, now: datetime) -> None: ...
    def enqueue_wakeup(
        self,
        session: Session,
        job_id: UUID,
        now: datetime,
        stale_after: timedelta,
        wakeup_interval: timedelta,
    ) -> None: ...
