from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import func, insert, select, update
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.errors.execution import terminal_reason_for_failure
from intelligent_reimbursement_assitant.execution_state_service.adapters.events import append_status
from intelligent_reimbursement_assitant.execution_state_service.adapters.schema import (
    execution_run,
    outbox_event,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.events import JobStatusEvent
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ClaimDeferred,
    Claimed,
    ClaimResult,
    ClaimSkipped,
    ExecutionContext,
    ExecutionKind,
    ExecutionSettings,
    ExecutionStatus,
    QueueReason,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.ownership import (
    require_execution_owner,
)

TERMINAL = {ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}


class ExecutionStateService:
    def _lock(self, session: Session, job_id: UUID) -> RowMapping | None:
        return (
            session.execute(
                select(execution_run).where(execution_run.c.job_id == job_id).with_for_update()
            )
            .mappings()
            .one_or_none()
        )

    def _now(self, session: Session) -> datetime:
        # transaction_timestamp()/now() can predate the wait for a row lock.
        return session.execute(select(func.clock_timestamp())).scalar_one()

    def _write(self, session: Session, job_id: UUID, **values: object) -> None:
        statement = update(execution_run).where(execution_run.c.job_id == job_id).values(**values)
        if "status" in values:
            row = (
                session.execute(
                    statement.returning(
                        *[execution_run.c[name] for name in JobStatusEvent.model_fields]
                    )
                )
                .mappings()
                .one()
            )
            append_status(session, job_id, JobStatusEvent.model_validate(row))
        else:
            session.execute(statement)

    def _exhausted(self, run: RowMapping, *, retry: bool) -> str | None:
        if retry and run["retry_count"] >= run["max_retries"]:
            return "Automatic retry limit exhausted"
        if run["budget_used"] + run["budget_reserved"] >= run["budget_limit"]:
            return "Execution budget exhausted or fully reserved"
        return None

    def claim(
        self,
        session: Session,
        job_id: UUID,
        worker_id: str,
        kind: ExecutionKind,
        lease_duration: timedelta,
    ) -> ClaimResult:
        run = self._lock(session, job_id)
        if run is None:
            raise LookupError(f"Job does not exist: {job_id}")
        now = self._now(session)
        if run["kind"] != kind:
            return ClaimDeferred(reason="KIND_MISMATCH")
        status = run["status"]
        if status in TERMINAL or status in {
            ExecutionStatus.RETRY_WAIT,
            ExecutionStatus.WAITING_INPUT,
        }:
            return ClaimSkipped(reason=status.value)
        if status in {ExecutionStatus.RUNNING, ExecutionStatus.CANCELLING}:
            if run["lease_expires_at"] is not None and run["lease_expires_at"] > now:
                return ClaimSkipped(reason="ACTIVE_LEASE")
            return ClaimDeferred(reason="LEASE_EXPIRED")
        if status != ExecutionStatus.QUEUED:
            return ClaimDeferred(reason="NOT_QUEUED")
        retry = run["queue_reason"] == QueueReason.AUTO_RETRY
        reason = self._exhausted(run, retry=retry)
        if reason is not None:
            self._write(
                session,
                job_id,
                status=ExecutionStatus.FAILED,
                queue_reason=None,
                terminal_reason=reason,
                finished_at=now,
                updated_at=now,
            )
            return ClaimSkipped(reason=ExecutionStatus.FAILED.value)
        epoch = run["lease_epoch"] + 1
        self._write(
            session,
            job_id,
            status=ExecutionStatus.RUNNING,
            queue_reason=None,
            lease_owner=worker_id,
            lease_epoch=epoch,
            lease_expires_at=now + lease_duration,
            retry_count=run["retry_count"] + int(retry),
            next_retry_at=None,
            updated_at=now,
            started_at=run["started_at"] or now,
        )
        return Claimed(context=ExecutionContext(job_id=job_id, lease_epoch=epoch))

    def _owned(
        self, session: Session, ctx: ExecutionContext, *, recovery: bool = False
    ) -> tuple[RowMapping, datetime]:
        run = self._lock(session, ctx.job_id)
        now = self._now(session)
        require_execution_owner(run, ctx, now, recovery=recovery)
        return cast(RowMapping, run), now

    def renew_lease(
        self, session: Session, ctx: ExecutionContext, lease_duration: timedelta
    ) -> ExecutionStatus:
        run, now = self._owned(session, ctx)
        self._write(session, ctx.job_id, lease_expires_at=now + lease_duration, updated_at=now)
        return run["status"]

    def check_execution(self, session: Session, ctx: ExecutionContext) -> bool:
        run, now = self._owned(session, ctx)
        if run["status"] == ExecutionStatus.CANCELLING:
            raise ExecutionError(
                ErrorCode.EXECUTION_CANCEL_REQUESTED, "ABORT", "Cancellation requested"
            )
        if reason := self._exhausted(run, retry=False):
            self._write(
                session,
                ctx.job_id,
                status=ExecutionStatus.FAILED,
                lease_owner=None,
                lease_expires_at=None,
                terminal_reason=reason,
                updated_at=now,
                finished_at=now,
            )
            return False
        return True

    def record_model_usage(self, session: Session, job_id: UUID, input_tokens: int) -> None:
        """Record the actual returned token usage once, even if execution has since stopped.

        This is accounting for an already issued request, not permission to execute.
        The caller never retries a transaction whose commit outcome is unknown.
        """
        run = self._lock(session, job_id)
        if run is None:
            raise LookupError(f"Job does not exist: {job_id}")
        self._write(session, job_id, budget_used=run["budget_used"] + input_tokens)

    def complete(self, session: Session, ctx: ExecutionContext) -> None:
        """Result is owned by business storage; caller commits publication in this transaction."""
        run, now = self._owned(session, ctx)
        if run["status"] == ExecutionStatus.CANCELLING:
            raise ExecutionError(
                ErrorCode.EXECUTION_CANCEL_REQUESTED, "ABORT", "Cancellation requested"
            )
        self._write(
            session,
            ctx.job_id,
            status=ExecutionStatus.SUCCEEDED,
            lease_owner=None,
            lease_expires_at=None,
            updated_at=now,
            finished_at=now,
            terminal_reason=None,
        )

    def wait_for_input(self, session: Session, ctx: ExecutionContext, waiting_ref: str) -> None:
        run, now = self._owned(session, ctx)
        if run["status"] == ExecutionStatus.CANCELLING:
            raise ExecutionError(
                ErrorCode.EXECUTION_CANCEL_REQUESTED, "ABORT", "Cancellation requested"
            )
        self._write(
            session,
            ctx.job_id,
            status=ExecutionStatus.WAITING_INPUT,
            lease_owner=None,
            lease_expires_at=None,
            updated_at=now,
        )

    def record_failure(
        self,
        session: Session,
        ctx: ExecutionContext,
        error: ExecutionError,
        *,
        recovery: bool = False,
        retry_delay: timedelta = timedelta(seconds=30),
    ) -> None:
        run, now = self._owned(session, ctx, recovery=recovery)
        if run["status"] == ExecutionStatus.CANCELLING and not (
            recovery
            and error.code == ErrorCode.EXECUTION_LEASE_EXPIRED
            and error.category == "OUTCOME_UNKNOWN"
        ):
            raise ExecutionError(
                ErrorCode.EXECUTION_CANCEL_REQUESTED, "ABORT", "Cancellation needs stop proof"
            )
        if error.category == "ABORT" and self._exhausted(run, retry=False) is None:
            raise error
        reason = self._exhausted(run, retry=True)
        retry = error.category == "TASK_RETRYABLE" and reason is None
        self._write(
            session,
            ctx.job_id,
            status=ExecutionStatus.RETRY_WAIT if retry else ExecutionStatus.FAILED,
            next_retry_at=now + retry_delay if retry else None,
            lease_owner=None,
            lease_expires_at=None,
            updated_at=now,
            last_error_code=error.code,
            terminal_reason=None if retry else terminal_reason_for_failure(error, reason),
            finished_at=None if retry else now,
        )

    def request_cancel(
        self, session: Session, job_id: UUID, *, kind: ExecutionKind | None = None
    ) -> bool:
        run = self._lock(session, job_id)
        if run is None or (kind is not None and run["kind"] != kind):
            return False
        if run["status"] in TERMINAL:
            return True
        now = self._now(session)
        if run["status"] in {ExecutionStatus.RUNNING, ExecutionStatus.CANCELLING}:
            if run["status"] == ExecutionStatus.RUNNING:
                self._write(session, job_id, status=ExecutionStatus.CANCELLING, updated_at=now)
        else:
            self._write(
                session,
                job_id,
                status=ExecutionStatus.CANCELLED,
                queue_reason=None,
                next_retry_at=None,
                lease_owner=None,
                lease_expires_at=None,
                finished_at=now,
                updated_at=now,
            )
        return True

    def confirm_cancel(
        self, session: Session, ctx: ExecutionContext, *, recovery: bool = False
    ) -> None:
        """Caller supplies actual stopping proof; lease expiry is not that proof."""
        run, now = self._owned(session, ctx, recovery=recovery)
        if run["status"] != ExecutionStatus.CANCELLING:
            raise ExecutionError(
                ErrorCode.EXECUTION_LEASE_LOST, "ABORT", "Cancellation is no longer current"
            )
        self._write(
            session,
            ctx.job_id,
            status=ExecutionStatus.CANCELLED,
            lease_owner=None,
            lease_expires_at=None,
            updated_at=now,
            finished_at=now,
        )

    def revoke_expired(
        self, session: Session, ctx: ExecutionContext, status: ExecutionStatus
    ) -> ExecutionContext | None:
        run = self._lock(session, ctx.job_id)
        now = self._now(session)
        if (
            run is None
            or run["status"] != status
            or run["lease_epoch"] != ctx.lease_epoch
            or (run["lease_expires_at"] is not None and run["lease_expires_at"] > now)
        ):
            return None
        epoch = ctx.lease_epoch + 1
        # Keep an expired timestamp: a crash during reconciliation remains discoverable.
        # Advancing it to now also rotates unresolved candidates through bounded scans.
        self._write(
            session,
            ctx.job_id,
            lease_epoch=epoch,
            lease_owner=None,
            lease_expires_at=now,
            updated_at=now,
        )
        return ExecutionContext(job_id=ctx.job_id, lease_epoch=epoch)

    def enqueue_initial(
        self,
        session: Session,
        settings: ExecutionSettings,
        *,
        kind: ExecutionKind,
        job_id: UUID | None = None,
    ) -> UUID:
        """Participate in the caller's transaction; never commit it here."""
        job_id = job_id or uuid4()
        now = datetime.now(UTC)
        session.execute(
            insert(execution_run).values(
                job_id=job_id,
                kind=kind,
                status=ExecutionStatus.QUEUED,
                queue_reason=QueueReason.INITIAL,
                retry_count=0,
                max_retries=settings.max_retries,
                next_retry_at=None,
                lease_owner=None,
                lease_epoch=0,
                lease_expires_at=None,
                budget_limit=settings.budget_limit,
                budget_used=Decimal(0),
                budget_reserved=Decimal(0),
                last_error_code=None,
                terminal_reason=None,
                created_at=now,
                updated_at=now,
                started_at=None,
                finished_at=None,
            )
        )
        session.execute(
            insert(outbox_event).values(
                event_id=uuid4(),
                job_id=job_id,
                kind=kind,
                created_at=now,
                published_at=None,
            )
        )
        append_status(
            session,
            job_id,
            JobStatusEvent(
                status=ExecutionStatus.QUEUED,
                next_retry_at=None,
                last_error_code=None,
                terminal_reason=None,
            ),
        )
        return job_id

    def enqueue(self, session: Session, job_id: UUID, now: datetime) -> None:
        """Approve a due AUTO_RETRY in the caller's short transaction."""
        run = (
            session.execute(
                select(execution_run).where(execution_run.c.job_id == job_id).with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if (
            run is None
            or run["status"] != ExecutionStatus.RETRY_WAIT
            or run["next_retry_at"] is None
            or run["next_retry_at"] > now
        ):
            return
        reason = None
        if run["retry_count"] >= run["max_retries"]:
            reason = "Automatic retry limit exhausted"
        elif run["budget_used"] + run["budget_reserved"] >= run["budget_limit"]:
            reason = "Execution budget exhausted or fully reserved"
        values: dict[str, object] = dict(
            next_retry_at=None, lease_owner=None, lease_expires_at=None, updated_at=now
        )
        if reason is not None:
            values.update(
                status=ExecutionStatus.FAILED,
                queue_reason=None,
                terminal_reason=reason,
                finished_at=now,
            )
        else:
            values.update(status=ExecutionStatus.QUEUED, queue_reason=QueueReason.AUTO_RETRY)
        self._write(session, job_id, **values)
        if reason is None:
            self._insert_event(session, job_id, run["kind"], now)

    def enqueue_wakeup(
        self,
        session: Session,
        job_id: UUID,
        now: datetime,
        stale_after: timedelta,
        wakeup_interval: timedelta,
    ) -> None:
        """Only create a notification; leave every ExecutionRun field unchanged."""
        run = (
            session.execute(
                select(execution_run).where(execution_run.c.job_id == job_id).with_for_update()
            )
            .mappings()
            .one_or_none()
        )
        if (
            run is None
            or run["status"] != ExecutionStatus.QUEUED
            or now - run["updated_at"] < stale_after
        ):
            return
        latest = session.scalar(
            select(func.max(outbox_event.c.created_at)).where(outbox_event.c.job_id == job_id)
        )
        if latest is not None and now - latest < wakeup_interval:
            return
        self._insert_event(session, job_id, run["kind"], now)

    def _insert_event(
        self, session: Session, job_id: UUID, kind: ExecutionKind, now: datetime
    ) -> None:
        session.execute(
            insert(outbox_event).values(
                event_id=uuid4(), job_id=job_id, kind=kind, created_at=now, published_at=None
            )
        )

    def resume_input(self, session: Session, job_id: UUID) -> None:
        run = self._lock(session, job_id)
        if run is None or run["status"] != ExecutionStatus.WAITING_INPUT:
            raise ValueError("Job is not waiting for input")
        now = self._now(session)
        self._write(
            session,
            job_id,
            status=ExecutionStatus.QUEUED,
            queue_reason=QueueReason.INPUT_RESUME,
            updated_at=now,
        )
        self._insert_event(session, job_id, run["kind"], now)
