"""Shared PostgreSQL task tables. No task state is duplicated in ingestion."""

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    Column,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    Table,
    Text,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB

from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionKind,
    ExecutionStatus,
    QueueReason,
)

metadata = MetaData()
execution_kind = Enum(ExecutionKind, name="execution_kind")
execution_status = Enum(ExecutionStatus, name="execution_status")
queue_reason = Enum(QueueReason, name="queue_reason")

execution_run = Table(
    "execution_run",
    metadata,
    Column("job_id", Uuid, primary_key=True),
    Column("kind", execution_kind, nullable=False),
    Column("status", execution_status, nullable=False),
    Column("queue_reason", queue_reason),
    Column("retry_count", Integer, nullable=False),
    Column("max_retries", Integer, nullable=False),
    Column("next_retry_at", DateTime(timezone=True)),
    Column("lease_owner", Text),
    Column("lease_epoch", BigInteger, nullable=False),
    Column("lease_expires_at", DateTime(timezone=True)),
    Column("budget_limit", Numeric, nullable=False),
    Column("budget_used", Numeric, nullable=False),
    Column("budget_reserved", Numeric, nullable=False),
    Column("last_error_code", Text),
    Column("terminal_reason", Text),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
    CheckConstraint(
        "status != 'QUEUED' OR queue_reason IS NOT NULL", name="queued_reason_required"
    ),
    CheckConstraint(
        "retry_count >= 0 AND max_retries >= 0 AND lease_epoch >= 0", name="execution_counts"
    ),
    CheckConstraint(
        "budget_limit >= 0 AND budget_used >= 0 AND budget_reserved >= 0", name="execution_budget"
    ),
)
outbox_event = Table(
    "outbox_event",
    metadata,
    Column("event_id", Uuid, primary_key=True),
    Column("job_id", Uuid, ForeignKey("execution_run.job_id"), nullable=False),
    Column("kind", execution_kind, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("published_at", DateTime(timezone=True)),
)

execution_event = Table(
    "execution_event",
    metadata,
    Column("job_id", Uuid, ForeignKey("execution_run.job_id"), primary_key=True),
    Column("sequence", BigInteger, primary_key=True),
    Column("event_type", Text, nullable=False),
    Column("payload", JSONB, nullable=False),
    CheckConstraint("sequence > 0", name="execution_event_sequence_positive"),
)

Index(
    "ix_execution_due_retry",
    execution_run.c.next_retry_at,
    execution_run.c.job_id,
    postgresql_where=text("status = 'RETRY_WAIT'"),
)
Index(
    "ix_execution_queued_age",
    execution_run.c.updated_at,
    execution_run.c.job_id,
    postgresql_where=text("status = 'QUEUED'"),
)
Index(
    "ix_outbox_pending",
    outbox_event.c.kind,
    outbox_event.c.created_at,
    outbox_event.c.event_id,
    postgresql_where=outbox_event.c.published_at.is_(None),
)
Index("ix_outbox_job_created", outbox_event.c.job_id, outbox_event.c.created_at.desc())
