from datetime import timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session

from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ClaimResult,
    ExecutionContext,
    ExecutionKind,
    ExecutionStatus,
)


class ExecutionRepository(Protocol):
    def session(self) -> Session: ...


class ExecutionControl(Protocol):
    def claim(
        self,
        session: Session,
        job_id: UUID,
        worker_id: str,
        kind: ExecutionKind,
        lease_duration: timedelta,
    ) -> ClaimResult: ...
    def renew_lease(
        self, session: Session, ctx: ExecutionContext, lease_duration: timedelta
    ) -> ExecutionStatus: ...
    def wait_for_input(self, session: Session, ctx: ExecutionContext, waiting_ref: str) -> None: ...
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
