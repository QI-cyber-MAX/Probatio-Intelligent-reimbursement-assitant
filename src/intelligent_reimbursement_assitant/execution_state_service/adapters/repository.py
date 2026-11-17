"""Bounded maintenance reads and short Outbox confirmation transactions."""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import Engine, func, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from intelligent_reimbursement_assitant.execution_state_service.adapters.schema import (
    execution_run,
    outbox_event,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionContext,
    ExecutionKind,
    ExecutionStatus,
    OutboxEvent,
)


class SQLAlchemyExecutionRepository:
    def __init__(self, engine: Engine):
        self.sessions = sessionmaker(engine.execution_options(isolation_level="READ COMMITTED"))

    def session(self) -> Session:
        return self.sessions()

    def list_expired_candidates(
        self, status: ExecutionStatus, now: datetime, limit: int
    ) -> list[tuple[ExecutionContext, ExecutionKind]]:
        with self.session() as session:
            rows = session.execute(
                select(execution_run.c.job_id, execution_run.c.lease_epoch, execution_run.c.kind)
                .where(
                    execution_run.c.status == status,
                    or_(
                        execution_run.c.lease_expires_at <= now,
                        execution_run.c.lease_expires_at.is_(None),
                    ),
                )
                .order_by(
                    execution_run.c.lease_expires_at.nullsfirst(),
                    execution_run.c.updated_at,
                    execution_run.c.job_id,
                )
                .limit(limit)
            ).mappings()
            return [
                (
                    ExecutionContext(job_id=row["job_id"], lease_epoch=row["lease_epoch"]),
                    row["kind"],
                )
                for row in rows
            ]

    def list_retry_candidates(self, now: datetime, limit: int) -> list[UUID]:
        with self.session() as session:
            return list(
                session.scalars(
                    select(execution_run.c.job_id)
                    .where(
                        execution_run.c.status == ExecutionStatus.RETRY_WAIT,
                        execution_run.c.next_retry_at <= now,
                    )
                    .order_by(execution_run.c.next_retry_at, execution_run.c.job_id)
                    .limit(limit)
                )
            )

    def list_wakeup_candidates(
        self, now: datetime, stale_after: timedelta, wakeup_interval: timedelta, limit: int
    ) -> list[UUID]:
        latest = (
            select(func.max(outbox_event.c.created_at))
            .where(outbox_event.c.job_id == execution_run.c.job_id)
            .scalar_subquery()
        )
        with self.session() as session:
            return list(
                session.scalars(
                    select(execution_run.c.job_id)
                    .where(
                        execution_run.c.status == ExecutionStatus.QUEUED,
                        execution_run.c.updated_at <= now - stale_after,
                        or_(latest.is_(None), latest <= now - wakeup_interval),
                    )
                    .order_by(execution_run.c.updated_at, execution_run.c.job_id)
                    .limit(limit)
                )
            )

    def list_pending_events(self, kind: ExecutionKind, limit: int) -> list[OutboxEvent]:
        with self.session() as session:
            rows = session.execute(
                select(outbox_event)
                .where(outbox_event.c.kind == kind, outbox_event.c.published_at.is_(None))
                .order_by(outbox_event.c.created_at, outbox_event.c.event_id)
                .limit(limit)
            ).mappings()
            return [OutboxEvent.model_validate(row) for row in rows]

    def mark_published(self, event_id: UUID, now: datetime) -> None:
        with self.session() as session, session.begin():
            session.execute(
                update(outbox_event)
                .where(outbox_event.c.event_id == event_id, outbox_event.c.published_at.is_(None))
                .values(published_at=now)
            )
