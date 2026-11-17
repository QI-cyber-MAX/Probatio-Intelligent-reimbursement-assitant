from sqlalchemy import Column, ForeignKey, Index, Table, Uuid
from sqlalchemy.dialects.postgresql import JSONB

from intelligent_reimbursement_assitant.execution_state_service.adapters.schema import metadata

wiki_build = Table(
    "wiki_build",
    metadata,
    Column("job_id", Uuid, ForeignKey("execution_run.job_id"), primary_key=True),
    Column("kb_id", Uuid, ForeignKey("knowledge_base.kb_id"), nullable=False),
    Column("input", JSONB, nullable=False),
    Column("checkpoint", JSONB, nullable=False),
)
Index("ix_wiki_build_kb", wiki_build.c.kb_id, wiki_build.c.job_id)
