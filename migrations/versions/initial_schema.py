"""Create the current business schema in an empty PostgreSQL database."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "initial_schema"
down_revision = None
branch_labels = None
depends_on = None

execution_kind = postgresql.ENUM(
    "INGESTION", "AGENT_TURN", "WIKI_BUILD", name="execution_kind", create_type=False
)
execution_status = postgresql.ENUM(
    "ACCEPTED",
    "QUEUED",
    "RUNNING",
    "RETRY_WAIT",
    "WAITING_INPUT",
    "CANCELLING",
    "SUCCEEDED",
    "FAILED",
    "CANCELLED",
    name="execution_status",
    create_type=False,
)
queue_reason = postgresql.ENUM(
    "INITIAL", "AUTO_RETRY", "INPUT_RESUME", name="queue_reason", create_type=False
)
ingestion_stage = postgresql.ENUM(
    "REGISTERED",
    "PARSING",
    "NORMALIZING",
    "CHUNKING",
    "EMBEDDING",
    "INDEXING",
    "PUBLISHING",
    name="ingestion_stage",
    create_type=False,
)


def upgrade() -> None:
    execution_kind.create(op.get_bind(), checkfirst=False)
    execution_status.create(op.get_bind(), checkfirst=False)
    queue_reason.create(op.get_bind(), checkfirst=False)
    ingestion_stage.create(op.get_bind(), checkfirst=False)
    op.create_table(
        "execution_run",
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("kind", execution_kind, nullable=False),
        sa.Column("status", execution_status, nullable=False),
        sa.Column("queue_reason", queue_reason, nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False),
        sa.Column("max_retries", sa.Integer(), nullable=False),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.Text(), nullable=True),
        sa.Column("lease_epoch", sa.BigInteger(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("budget_limit", sa.Numeric(), nullable=False),
        sa.Column("budget_used", sa.Numeric(), nullable=False),
        sa.Column("budget_reserved", sa.Numeric(), nullable=False),
        sa.Column("last_error_code", sa.Text(), nullable=True),
        sa.Column("terminal_reason", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status != 'QUEUED' OR queue_reason IS NOT NULL", name="queued_reason_required"
        ),
        sa.CheckConstraint(
            "budget_limit >= 0 AND budget_used >= 0 AND budget_reserved >= 0",
            name="execution_budget",
        ),
        sa.CheckConstraint(
            "retry_count >= 0 AND max_retries >= 0 AND lease_epoch >= 0", name="execution_counts"
        ),
        sa.PrimaryKeyConstraint("job_id"),
    )
    op.create_table(
        "knowledge_base",
        sa.Column("kb_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("active_index_profile_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_wiki_job_id", sa.Uuid(), nullable=True),
        sa.PrimaryKeyConstraint("kb_id"),
    )
    op.create_table(
        "document",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("kb_id", sa.Uuid(), nullable=False),
        sa.Column("issuer", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("policy_identity", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["kb_id"], ["knowledge_base.kb_id"]),
        sa.PrimaryKeyConstraint("document_id"),
        sa.UniqueConstraint("kb_id", "issuer", "title", name="document_identity"),
    )
    op.create_table(
        "outbox_event",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("kind", execution_kind, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["job_id"], ["execution_run.job_id"]),
        sa.PrimaryKeyConstraint("event_id"),
    )
    op.create_table(
        "document_version",
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("source_hash", sa.Text(), nullable=False),
        sa.Column("source_ref", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("policy_metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("serving_generation_id", sa.Uuid(), nullable=True),
        sa.ForeignKeyConstraint(["document_id"], ["document.document_id"]),
        sa.PrimaryKeyConstraint("document_id", "source_hash"),
    )
    op.create_table(
        "ingestion_job",
        sa.Column("ingestion_run_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("source_hash", sa.Text(), nullable=False),
        sa.Column("stage", ingestion_stage, nullable=False),
        sa.Column("checkpoint", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.ForeignKeyConstraint(
            ["document_id", "source_hash"],
            ["document_version.document_id", "document_version.source_hash"],
        ),
        sa.ForeignKeyConstraint(["ingestion_run_id"], ["execution_run.job_id"]),
        sa.PrimaryKeyConstraint("ingestion_run_id"),
    )
    op.create_index(
        "ix_execution_due_retry",
        "execution_run",
        ["next_retry_at", "job_id"],
        postgresql_where=sa.text("status = 'RETRY_WAIT'"),
    )
    op.create_index(
        "ix_execution_queued_age",
        "execution_run",
        ["updated_at", "job_id"],
        postgresql_where=sa.text("status = 'QUEUED'"),
    )
    op.create_index(
        "ix_outbox_pending",
        "outbox_event",
        ["kind", "created_at", "event_id"],
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.create_index("ix_outbox_job_created", "outbox_event", ["job_id", sa.text("created_at DESC")])
    op.create_table(
        "conversation",
        sa.Column("conversation_id", sa.Uuid, primary_key=True),
        sa.Column("active_job_id", sa.Uuid, sa.ForeignKey("execution_run.job_id")),
    )
    op.create_table(
        "conversation_message",
        sa.Column("message_id", sa.Uuid, primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Uuid,
            sa.ForeignKey("conversation.conversation_id"),
            nullable=False,
        ),
        sa.Column("reply_to_message_id", sa.Uuid, sa.ForeignKey("conversation_message.message_id")),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "agent_turn",
        sa.Column("qa_run_id", sa.Uuid, sa.ForeignKey("execution_run.job_id"), primary_key=True),
        sa.Column(
            "conversation_id",
            sa.Uuid,
            sa.ForeignKey("conversation.conversation_id"),
            nullable=False,
        ),
        sa.Column(
            "message_id",
            sa.Uuid,
            sa.ForeignKey("conversation_message.message_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("input_ref", sa.Text, nullable=False),
        sa.Column("answer_ref", sa.Text),
    )
    op.create_index(
        "conversation_message_history",
        "conversation_message",
        ["conversation_id", "created_at", "message_id"],
    )
    op.create_index(
        "conversation_message_one_answer",
        "conversation_message",
        ["reply_to_message_id"],
        unique=True,
        postgresql_where=sa.text("payload->>'kind' = 'answer'"),
    )
    op.create_index(
        "conversation_message_one_reply",
        "conversation_message",
        ["reply_to_message_id"],
        unique=True,
        postgresql_where=sa.text("payload->>'kind' = 'clarification_reply'"),
    )
    op.create_index(
        "conversation_message_one_interrupt",
        "conversation_message",
        ["reply_to_message_id", sa.text("(payload->>'interrupt_id')")],
        unique=True,
        postgresql_where=sa.text("payload->>'kind' = 'clarification_request'"),
    )
    op.create_table(
        "execution_event",
        sa.Column("job_id", sa.Uuid, sa.ForeignKey("execution_run.job_id"), primary_key=True),
        sa.Column("sequence", sa.BigInteger, primary_key=True),
        sa.Column("event_type", sa.Text, nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.CheckConstraint("sequence > 0", name="execution_event_sequence_positive"),
    )
    op.create_index("ix_ingestion_source", "ingestion_job", ["document_id", "source_hash"])
    op.create_table(
        "wiki_build",
        sa.Column("job_id", sa.Uuid, sa.ForeignKey("execution_run.job_id"), primary_key=True),
        sa.Column("kb_id", sa.Uuid, sa.ForeignKey("knowledge_base.kb_id"), nullable=False),
        sa.Column("input", postgresql.JSONB, nullable=False),
        sa.Column("checkpoint", postgresql.JSONB, nullable=False),
    )
    op.create_index("ix_wiki_build_kb", "wiki_build", ["kb_id", "job_id"])
    op.create_foreign_key(
        "fk_published_wiki", "knowledge_base", "wiki_build", ["published_wiki_job_id"], ["job_id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_published_wiki", "knowledge_base", type_="foreignkey")
    op.drop_table("wiki_build")
    op.drop_table("execution_event")
    op.drop_table("agent_turn")
    op.drop_table("conversation_message")
    op.drop_table("conversation")
    op.drop_table("ingestion_job")
    op.drop_table("document_version")
    op.drop_table("outbox_event")
    op.drop_table("document")
    op.drop_table("knowledge_base")
    op.drop_table("execution_run")
    ingestion_stage.drop(op.get_bind(), checkfirst=False)
    queue_reason.drop(op.get_bind(), checkfirst=False)
    execution_status.drop(op.get_bind(), checkfirst=False)
    execution_kind.drop(op.get_bind(), checkfirst=False)
