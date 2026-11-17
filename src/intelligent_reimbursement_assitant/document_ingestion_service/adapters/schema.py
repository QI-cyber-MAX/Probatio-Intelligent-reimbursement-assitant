"""Document registration tables, with logical identity and composite source identity constraints."""

from sqlalchemy import (
    Column,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB

from intelligent_reimbursement_assitant.execution_state_service.adapters.schema import metadata
from intelligent_reimbursement_assitant.execution_state_service.domain.models import IngestionStage

knowledge_base = Table(
    "knowledge_base",
    metadata,
    Column("kb_id", Uuid, primary_key=True),
    Column("name", Text, nullable=False),
    Column("revision", Integer, nullable=False),
    Column("active_index_profile_id", Text, nullable=False),
    Column(
        "published_wiki_job_id",
        Uuid,
        ForeignKey("wiki_build.job_id", use_alter=True, name="fk_published_wiki"),
    ),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
document = Table(
    "document",
    metadata,
    Column("document_id", Uuid, primary_key=True),
    Column("kb_id", Uuid, ForeignKey("knowledge_base.kb_id"), nullable=False),
    Column("issuer", Text, nullable=False),
    Column("title", Text, nullable=False),
    Column("policy_identity", Text),
    UniqueConstraint("kb_id", "issuer", "title", name="document_identity"),
)
document_version = Table(
    "document_version",
    metadata,
    Column("document_id", Uuid, ForeignKey("document.document_id"), primary_key=True),
    Column("source_hash", Text, primary_key=True),
    Column("source_ref", JSONB, nullable=False),
    Column("uploaded_at", DateTime(timezone=True), nullable=False),
    Column("policy_metadata", JSONB, nullable=False),
    Column("serving_generation_id", Uuid),
)
ingestion_job = Table(
    "ingestion_job",
    metadata,
    Column("ingestion_run_id", Uuid, ForeignKey("execution_run.job_id"), primary_key=True),
    Column("document_id", Uuid, nullable=False),
    Column("source_hash", Text, nullable=False),
    Column("stage", Enum(IngestionStage, name="ingestion_stage"), nullable=False),
    Column("checkpoint", JSONB, nullable=False),
    ForeignKeyConstraint(
        ["document_id", "source_hash"],
        ["document_version.document_id", "document_version.source_hash"],
    ),
)

Index("ix_ingestion_source", ingestion_job.c.document_id, ingestion_job.c.source_hash)
