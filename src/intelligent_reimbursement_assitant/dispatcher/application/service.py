import logging
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta
from uuid import UUID

from psycopg import Error as PsycopgError
from sqlalchemy.exc import SQLAlchemyError

from intelligent_reimbursement_assitant.dispatcher.ports.scheduling import (
    SchedulingRepository,
    SchedulingState,
)
from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.errors.execution import exception_diagnostic
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionKind,
    ExecutionStatus,
)

logger = logging.getLogger(__name__)


class Dispatcher:
    def __init__(
        self,
        repository: SchedulingRepository,
        state: SchedulingState,
        *,
        batch_size: int = 20,
        stale_after: timedelta = timedelta(seconds=300),
        wakeup_interval: timedelta = timedelta(seconds=300),
        stopping: Callable[[], bool] = lambda: False,
        completion_checks: Mapping[ExecutionKind, Callable[[UUID], bool]] | None = None,
    ):
        self.repository = repository
        self.state = state
        self.batch_size = batch_size
        self.stale_after = stale_after
        self.wakeup_interval = wakeup_interval
        self.stopping = stopping
        self.completion_checks = completion_checks or {}

    def tick(self, now: datetime) -> None:
        if self.stopping():
            return
        self._recover_expired(now)
        for job_id in self.repository.list_retry_candidates(now, self.batch_size):
            if self.stopping():
                return
            try:
                with self.repository.session() as session, session.begin():
                    self.state.enqueue(session, job_id, now)
            except (SQLAlchemyError, PsycopgError) as error:
                logger.error(
                    "Retry scheduling failed for job_id=%s %s", job_id, exception_diagnostic(error)
                )
        if self.stopping():
            return
        for job_id in self.repository.list_wakeup_candidates(
            now, self.stale_after, self.wakeup_interval, self.batch_size
        ):
            if self.stopping():
                return
            try:
                with self.repository.session() as session, session.begin():
                    self.state.enqueue_wakeup(
                        session, job_id, now, self.stale_after, self.wakeup_interval
                    )
            except (SQLAlchemyError, PsycopgError) as error:
                logger.error(
                    "Wakeup scheduling failed for job_id=%s %s", job_id, exception_diagnostic(error)
                )

    def _recover_expired(self, now: datetime) -> None:
        for status in (ExecutionStatus.RUNNING, ExecutionStatus.CANCELLING):
            if self.stopping():
                return
            for observed, kind in self.repository.list_expired_candidates(
                status, now, self.batch_size
            ):
                if self.stopping():
                    return
                try:
                    with self.repository.session() as session, session.begin():
                        ctx = self.state.revoke_expired(session, observed, status)
                    if ctx is None:
                        continue
                    check = self.completion_checks.get(kind)
                    if check is None:
                        logger.warning(
                            "Expired lease fenced; completion check unavailable job_id=%s",
                            ctx.job_id,
                        )
                        continue
                    try:
                        if not check(ctx.job_id):
                            continue
                    except ExecutionError as error:
                        with self.repository.session() as session, session.begin():
                            if (
                                error.code == ErrorCode.EXECUTION_CANCEL_REQUESTED
                                and error.category == "ABORT"
                            ):
                                self.state.confirm_cancel(session, ctx, recovery=True)
                            elif error.category != "ABORT":
                                self.state.record_failure(session, ctx, error, recovery=True)
                        continue
                except (SQLAlchemyError, PsycopgError, ExecutionError) as error:
                    logger.error(
                        "Expired lease recovery deferred job_id=%s %s",
                        observed.job_id,
                        exception_diagnostic(error),
                    )
