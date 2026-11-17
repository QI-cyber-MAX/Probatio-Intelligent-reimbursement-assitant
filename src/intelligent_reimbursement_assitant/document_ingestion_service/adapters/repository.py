"""READ COMMITTED business transactions and a dedicated publication-lock connection."""

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from pydantic import JsonValue
from sqlalchemy import Engine, Text, func, insert, select, true, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from intelligent_reimbursement_assitant.contracts.models import ArtifactRef, SourceReference
from intelligent_reimbursement_assitant.document_ingestion_service.adapters.schema import (
    document,
    document_version,
    ingestion_job,
    knowledge_base,
)
from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    AcceptedUpload,
    Document,
    DocumentRegistration,
    DocumentSourceView,
    DocumentView,
    IngestionAccepted,
    KnowledgeBase,
    KnowledgeSnapshot,
    PolicyDate,
    PolicyMetadata,
    SnapshotEntry,
)
from intelligent_reimbursement_assitant.document_ingestion_service.ports.repository import (
    CompletionFacts,
    DocumentItem,
    IngestionSource,
    KnowledgeBaseItem,
    PublishedSourceArtifacts,
)
from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.execution_state_service.adapters.events import (
    event_high_watermark,
)
from intelligent_reimbursement_assitant.execution_state_service.adapters.schema import execution_run
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionKind,
    ExecutionStatus,
    IngestionJobView,
    IngestionStage,
)


class SQLAlchemyDocumentRepository:
    def __init__(self, engine: Engine):
        # Conflict losers read a new statement snapshot after ON CONFLICT waits.
        self.engine = engine.execution_options(isolation_level="READ COMMITTED")
        self.sessions = sessionmaker(self.engine)

    def session(self) -> Session:
        return self.sessions()

    def load_published_artifacts(
        self, session: Session, snapshot: KnowledgeSnapshot
    ) -> dict[UUID, PublishedSourceArtifacts]:
        wanted = {str(entry.generation_id): entry for entry in snapshot.entries}
        rows = session.execute(
            select(
                document_version.c.document_id,
                document_version.c.source_hash,
                document_version.c.source_ref,
                ingestion_job.c.checkpoint,
            )
            .select_from(document_version.join(ingestion_job))
            .where(ingestion_job.c.checkpoint["generation_id"].astext.in_(wanted))
        ).mappings()
        result = {}
        for row in rows:
            checkpoint = row["checkpoint"]
            entry = wanted[checkpoint["generation_id"]]
            if (row["document_id"], row["source_hash"]) != (
                entry.source.document_id,
                entry.source.source_hash,
            ):
                continue
            try:
                result[entry.generation_id] = PublishedSourceArtifacts(
                    source_ref=row["source_ref"],
                    parsed_document_ref=checkpoint["parsed_document_ref"],
                    chunk_batch_ref=checkpoint["chunk_batch_ref"],
                )
            except (KeyError, ValueError) as error:
                raise ExecutionError(
                    ErrorCode.WIKI_SOURCE_UNAVAILABLE,
                    "PERMANENT",
                    "Published artifacts unavailable",
                ) from error
        if result.keys() != {entry.generation_id for entry in snapshot.entries}:
            raise ExecutionError(
                ErrorCode.WIKI_SOURCE_UNAVAILABLE,
                "PERMANENT",
                "Published generation artifacts missing",
            )
        return result

    def load_knowledge_snapshot(self, session: Session, kb_id: UUID) -> KnowledgeSnapshot | None:
        published = (
            select(
                document.c.kb_id,
                document.c.title,
                document_version.c.document_id,
                document_version.c.source_hash,
                document_version.c.serving_generation_id,
                document_version.c.policy_metadata,
                ingestion_job.c.checkpoint["index_profile_id"].astext.label("index_profile_id"),
            )
            .select_from(document.join(document_version).join(ingestion_job))
            .where(
                document_version.c.serving_generation_id.is_not(None),
                ingestion_job.c.checkpoint["generation_id"].astext
                == func.cast(document_version.c.serving_generation_id, Text),
            )
            .subquery()
        )
        rows = (
            session.execute(
                select(
                    knowledge_base.c.kb_id,
                    knowledge_base.c.revision,
                    *[
                        published.c[name]
                        for name in (
                            "document_id",
                            "title",
                            "source_hash",
                            "serving_generation_id",
                            "policy_metadata",
                            "index_profile_id",
                        )
                    ],
                )
                .select_from(
                    knowledge_base.outerjoin(published, knowledge_base.c.kb_id == published.c.kb_id)
                )
                .where(knowledge_base.c.kb_id == kb_id)
                .order_by(published.c.document_id, published.c.source_hash)
            )
            .mappings()
            .all()
        )
        if not rows:
            return None
        entries = [
            SnapshotEntry(
                title=row["title"],
                source=SourceReference(
                    document_id=row["document_id"], source_hash=row["source_hash"]
                ),
                generation_id=row["serving_generation_id"],
                index_profile_id=row["index_profile_id"],
                policy_metadata=PolicyMetadata.model_validate(row["policy_metadata"]),
            )
            for row in rows
            if row["document_id"] is not None
        ]
        return KnowledgeSnapshot(kb_id=kb_id, revision=rows[0]["revision"], entries=entries)

    @contextmanager
    def publication_scope(self, document_id: UUID) -> Iterator[Session | None]:
        key = int.from_bytes(
            hashlib.sha256(b"ingestion-publication:" + document_id.bytes).digest()[:8],
            signed=True,
        )
        with self.engine.connect() as connection:
            acquired = connection.scalar(select(func.pg_try_advisory_lock(key)))
            try:
                connection.commit()
                if not acquired:
                    yield None
                    return
                # Every transaction uses this connection. A disconnect escapes the scope;
                # no operation catches it and reconnects to continue publication.
                with Session(connection) as session:
                    yield session
            finally:
                if acquired:
                    try:
                        connection.execute(select(func.pg_advisory_unlock(key)))
                        connection.commit()
                    except BaseException:
                        connection.invalidate()
                        raise

    def load_publication_sources(
        self, session: Session, source: SourceReference
    ) -> tuple[Document, PolicyMetadata, list[SnapshotEntry]]:
        identity = (
            session.execute(
                select(document, document_version.c.policy_metadata)
                .select_from(document.join(document_version))
                .where(
                    document_version.c.document_id == source.document_id,
                    document_version.c.source_hash == source.source_hash,
                )
            )
            .mappings()
            .one()
        )
        policy = PolicyMetadata.model_validate(identity["policy_metadata"])
        logical = Document.model_validate(
            {column.name: identity[column.name] for column in document.c}
        )
        rows = session.execute(
            select(
                document_version.c.document_id,
                document_version.c.source_hash,
                document_version.c.serving_generation_id,
                document_version.c.policy_metadata,
                ingestion_job.c.checkpoint["index_profile_id"].astext.label("index_profile_id"),
            )
            .select_from(document_version.join(ingestion_job))
            .where(
                document_version.c.document_id == source.document_id,
                document_version.c.serving_generation_id.is_not(None),
                ingestion_job.c.checkpoint["generation_id"].astext
                == func.cast(document_version.c.serving_generation_id, Text),
            )
        ).mappings()
        entries = [
            SnapshotEntry(
                title=logical.title,
                source=SourceReference(
                    document_id=row["document_id"], source_hash=row["source_hash"]
                ),
                generation_id=row["serving_generation_id"],
                index_profile_id=row["index_profile_id"],
                policy_metadata=PolicyMetadata.model_validate(row["policy_metadata"]),
            )
            for row in rows
        ]
        return logical, policy, entries

    def update_policy_metadata(
        self, session: Session, source: SourceReference, metadata: PolicyMetadata
    ) -> None:
        session.execute(
            update(document_version)
            .where(
                document_version.c.document_id == source.document_id,
                document_version.c.source_hash == source.source_hash,
            )
            .values(policy_metadata=metadata.model_dump(mode="json"))
        )

    def publish_source(
        self, session: Session, source: SourceReference, generation_id: UUID
    ) -> None:
        session.execute(
            update(document_version)
            .where(
                document_version.c.document_id == source.document_id,
                document_version.c.source_hash == source.source_hash,
            )
            .values(serving_generation_id=generation_id)
        )

    def increment_knowledge_revision(self, session: Session, kb_id: UUID) -> None:
        session.execute(
            update(knowledge_base)
            .where(knowledge_base.c.kb_id == kb_id)
            .values(revision=knowledge_base.c.revision + 1)
        )

    def load_completion_facts(self, session: Session, job_id: UUID) -> CompletionFacts:
        row = (
            session.execute(
                select(
                    execution_run.c.status,
                    ingestion_job.c.stage,
                    ingestion_job.c.checkpoint,
                    ingestion_job.c.document_id,
                )
                .select_from(execution_run.join(ingestion_job))
                .where(execution_run.c.job_id == job_id)
            )
            .mappings()
            .one()
        )
        return CompletionFacts(
            status=ExecutionStatus(row["status"]),
            stage=IngestionStage(row["stage"]),
            checkpoint=row["checkpoint"],
            document_id=row["document_id"],
        )

    def knowledge_base_exists(self, session: Session, kb_id: UUID) -> bool:
        return (
            session.scalar(select(knowledge_base.c.kb_id).where(knowledge_base.c.kb_id == kb_id))
            is not None
        )

    def get_document(self, session: Session, document_id: UUID) -> Document | None:
        row = (
            session.execute(select(document).where(document.c.document_id == document_id))
            .mappings()
            .one_or_none()
        )
        return Document.model_validate(row) if row is not None else None

    def find_document(
        self, session: Session, registration: DocumentRegistration
    ) -> Document | None:
        row = (
            session.execute(
                select(document).where(
                    document.c.kb_id == registration.kb_id,
                    document.c.issuer == registration.issuer,
                    document.c.title == registration.title,
                )
            )
            .mappings()
            .one_or_none()
        )
        return Document.model_validate(row) if row is not None else None

    def find_latest_ingestion_accepted(
        self, session: Session, document_id: UUID, source_hash: str
    ) -> IngestionAccepted | None:
        row = (
            session.execute(
                select(
                    document_version.c.document_id,
                    document_version.c.source_hash,
                    ingestion_job.c.ingestion_run_id.label("job_id"),
                )
                .select_from(document_version.join(ingestion_job).join(execution_run))
                .where(
                    document_version.c.document_id == document_id,
                    document_version.c.source_hash == source_hash,
                )
                .order_by(execution_run.c.created_at.desc(), execution_run.c.job_id.desc())
                .limit(1)
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return IngestionAccepted(**row, status_url=f"/ingestion-jobs/{row['job_id']}")

    def create_knowledge_base(self, session: Session, value: KnowledgeBase) -> None:
        session.execute(insert(knowledge_base).values(**value.model_dump()))

    def find_job_identity(
        self, session: Session, job_id: UUID
    ) -> tuple[ExecutionKind, SourceReference | None] | None:
        row = session.execute(
            select(
                execution_run.c.kind,
                ingestion_job.c.document_id,
                ingestion_job.c.source_hash,
            )
            .select_from(execution_run.outerjoin(ingestion_job))
            .where(execution_run.c.job_id == job_id)
        ).one_or_none()
        if row is None:
            return None
        source = (
            SourceReference(document_id=row.document_id, source_hash=row.source_hash)
            if row.document_id is not None
            else None
        )
        return row.kind, source

    def lock_source(self, session: Session, source: SourceReference) -> bool:
        return (
            session.execute(
                select(document_version.c.document_id)
                .where(
                    document_version.c.document_id == source.document_id,
                    document_version.c.source_hash == source.source_hash,
                )
                .with_for_update()
            ).one_or_none()
            is not None
        )

    def load_source_parsed_ref(
        self, session: Session, source: SourceReference, generation_id: UUID | None
    ) -> ArtifactRef | None:
        query = (
            select(ingestion_job.c.checkpoint["parsed_document_ref"])
            .select_from(ingestion_job.join(execution_run))
            .where(
                ingestion_job.c.document_id == source.document_id,
                ingestion_job.c.source_hash == source.source_hash,
                ingestion_job.c.checkpoint["parsed_document_ref"].astext.is_not(None),
            )
        )
        if generation_id is not None:
            query = query.where(
                ingestion_job.c.checkpoint["generation_id"].astext == str(generation_id)
            )
        value = session.scalar(query.order_by(execution_run.c.created_at.desc()).limit(1))
        return ArtifactRef.model_validate(value) if value is not None else None

    def has_active_ingestion(self, session: Session, source: SourceReference) -> bool:
        return (
            session.scalar(
                select(ingestion_job.c.ingestion_run_id)
                .select_from(ingestion_job.join(execution_run))
                .where(
                    ingestion_job.c.document_id == source.document_id,
                    ingestion_job.c.source_hash == source.source_hash,
                    execution_run.c.status.not_in(
                        [
                            ExecutionStatus.SUCCEEDED,
                            ExecutionStatus.FAILED,
                            ExecutionStatus.CANCELLED,
                        ]
                    ),
                )
                .limit(1)
            )
            is not None
        )

    def get_source_file(
        self, session: Session, source: SourceReference
    ) -> tuple[str, ArtifactRef] | None:
        row = session.execute(
            select(document.c.title, document_version.c.source_ref)
            .select_from(document.join(document_version))
            .where(
                document_version.c.document_id == source.document_id,
                document_version.c.source_hash == source.source_hash,
            )
        ).one_or_none()
        return (row.title, ArtifactRef.model_validate(row.source_ref)) if row is not None else None

    def get_document_view(self, session: Session, document_id: UUID) -> DocumentView | None:
        latest = (
            select(
                execution_run.c.job_id,
                execution_run.c.status,
                ingestion_job.c.stage,
                execution_run.c.retry_count,
                execution_run.c.max_retries,
                execution_run.c.next_retry_at,
                execution_run.c.last_error_code,
                execution_run.c.terminal_reason,
            )
            .select_from(ingestion_job.join(execution_run))
            .where(
                ingestion_job.c.document_id == document_version.c.document_id,
                ingestion_job.c.source_hash == document_version.c.source_hash,
            )
            .order_by(execution_run.c.created_at.desc(), execution_run.c.job_id.desc())
            .limit(1)
            .lateral()
        )
        rows = (
            session.execute(
                select(
                    document.c.document_id,
                    document.c.kb_id,
                    document.c.issuer,
                    document.c.title,
                    document_version.c.source_hash,
                    document_version.c.uploaded_at,
                    document_version.c.policy_metadata,
                    document_version.c.serving_generation_id,
                    *latest.c,
                )
                .select_from(document.outerjoin(document_version).outerjoin(latest, true()))
                .where(document.c.document_id == document_id)
                .order_by(
                    document_version.c.policy_metadata["effective_from"]["value"]
                    .astext.desc()
                    .nulls_last(),
                    document_version.c.uploaded_at.desc(),
                    document_version.c.source_hash,
                )
            )
            .mappings()
            .all()
        )
        if not rows:
            return None
        return DocumentView(
            **{name: rows[0][name] for name in ("document_id", "kb_id", "issuer", "title")},
            versions=[
                DocumentSourceView(
                    **{
                        name: row[name]
                        for name in (
                            "source_hash",
                            "uploaded_at",
                            "policy_metadata",
                            "serving_generation_id",
                        )
                    },
                    latest_job=IngestionJobView.model_validate(
                        {name: row[name] for name in latest.c.keys()}
                    ),
                )
                for row in rows
                if row["source_hash"] is not None
            ],
        )

    def get_or_create_document(
        self, session: Session, registration: DocumentRegistration
    ) -> Document:
        row = (
            session.execute(
                pg_insert(document)
                .values(
                    document_id=uuid4(),
                    kb_id=registration.kb_id,
                    issuer=registration.issuer,
                    title=registration.title,
                )
                .on_conflict_do_nothing(
                    index_elements=[document.c.kb_id, document.c.issuer, document.c.title]
                )
                .returning(*document.c)
            )
            .mappings()
            .one_or_none()
        )
        if row is not None:
            return Document.model_validate(row)
        # A distinct SELECT gets the winner's committed row under READ COMMITTED.
        return cast(Document, self.find_document(session, registration))

    def insert_source_if_absent(
        self, session: Session, document_id: UUID, upload: AcceptedUpload, policy: PolicyMetadata
    ) -> bool:
        return (
            session.execute(
                pg_insert(document_version)
                .values(
                    document_id=document_id,
                    source_hash=upload.source_hash,
                    source_ref=upload.source_ref.model_dump(mode="json"),
                    uploaded_at=datetime.now(UTC),
                    policy_metadata=policy.model_dump(mode="json"),
                    serving_generation_id=None,
                )
                .on_conflict_do_nothing(
                    index_elements=[document_version.c.document_id, document_version.c.source_hash]
                )
                .returning(document_version.c.document_id)
            ).scalar_one_or_none()
            is not None
        )

    def create_ingestion_job(
        self,
        session: Session,
        job_id: UUID,
        document_id: UUID,
        source_hash: str,
        *,
        checkpoint: dict[str, JsonValue] | None = None,
        stage: IngestionStage = IngestionStage.REGISTERED,
    ) -> None:
        session.execute(
            insert(ingestion_job).values(
                ingestion_run_id=job_id,
                document_id=document_id,
                source_hash=source_hash,
                stage=stage,
                checkpoint=checkpoint or {},
            )
        )

    def find_source_key(self, session: Session, document_id: UUID, source_hash: str) -> str | None:
        return session.scalar(
            select(document_version.c.source_ref["key"].astext).where(
                document_version.c.document_id == document_id,
                document_version.c.source_hash == source_hash,
            )
        )

    def referenced_artifact_keys(self, session: Session) -> set[str]:
        return set(session.scalars(select(document_version.c.source_ref["key"].astext)).all())

    def list_knowledge_bases(self, session: Session) -> list[KnowledgeBaseItem]:
        rows = session.execute(
            select(knowledge_base.c.kb_id, knowledge_base.c.name).order_by(
                knowledge_base.c.name, knowledge_base.c.kb_id
            )
        ).mappings()
        return [KnowledgeBaseItem(kb_id=row["kb_id"], name=row["name"]) for row in rows]

    def list_documents(
        self, session: Session, kb_id: UUID, issuer: str | None, title: str | None
    ) -> list[DocumentItem]:
        query = select(document.c.document_id, document.c.title, document.c.issuer).where(
            document.c.kb_id == kb_id
        )
        if issuer is not None:
            query = query.where(document.c.issuer == issuer)
        if title is not None:
            query = query.where(document.c.title.contains(title, autoescape=True))
        rows = session.execute(query.order_by(document.c.title, document.c.document_id)).mappings()
        return [
            DocumentItem(document_id=row["document_id"], title=row["title"], issuer=row["issuer"])
            for row in rows
        ]

    def get_ingestion_job(self, session: Session, job_id: UUID) -> IngestionJobView | None:
        result = self.read_event_head(session, job_id)
        return result[0] if result is not None else None

    def read_event_head(
        self, session: Session, job_id: UUID
    ) -> tuple[IngestionJobView, int] | None:
        row = (
            session.execute(
                select(
                    execution_run.c.job_id,
                    execution_run.c.status,
                    ingestion_job.c.stage,
                    execution_run.c.retry_count,
                    execution_run.c.max_retries,
                    execution_run.c.next_retry_at,
                    execution_run.c.last_error_code,
                    execution_run.c.terminal_reason,
                    event_high_watermark(job_id).label("high_watermark"),
                )
                .select_from(execution_run.join(ingestion_job))
                .where(
                    execution_run.c.job_id == job_id,
                    execution_run.c.kind == ExecutionKind.INGESTION,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return IngestionJobView.model_validate(
            {key: row[key] for key in IngestionJobView.model_fields}
        ), row["high_watermark"]

    def load_ingestion_source(self, session: Session, job_id: UUID) -> IngestionSource:
        row = (
            session.execute(
                select(
                    ingestion_job.c.document_id,
                    document_version.c.source_ref,
                    ingestion_job.c.stage,
                    ingestion_job.c.checkpoint,
                )
                .select_from(ingestion_job.join(document_version))
                .where(ingestion_job.c.ingestion_run_id == job_id)
            )
            .mappings()
            .one()
        )
        return IngestionSource(
            document_id=row["document_id"],
            source_ref=ArtifactRef.model_validate(row["source_ref"]),
            stage=IngestionStage(row["stage"]),
            checkpoint=row["checkpoint"],
        )

    def save_parsed_checkpoint(
        self,
        session: Session,
        job_id: UUID,
        expected_stage: IngestionStage,
        target_stage: IngestionStage,
        parsed_ref: ArtifactRef | None,
        extracted: PolicyDate | None = None,
    ) -> None:
        # Caller holds the ExecutionRun row lock after checking its current epoch.
        statement = (
            update(ingestion_job)
            .where(
                ingestion_job.c.ingestion_run_id == job_id,
                ingestion_job.c.stage == expected_stage,
            )
            .values(stage=target_stage)
        )
        if parsed_ref is not None:
            statement = statement.values(
                checkpoint=ingestion_job.c.checkpoint.op("||")(
                    {
                        "parsed_document_ref": parsed_ref.model_dump(mode="json"),
                        "extracted_effective_from": extracted.model_dump(mode="json")
                        if extracted
                        else None,
                    }
                )
            )
        if (
            session.execute(
                statement.returning(ingestion_job.c.ingestion_run_id)
            ).scalar_one_or_none()
            is None
        ):
            raise RuntimeError("ingestion stage changed before checkpoint commit")

    def get_or_create_chunking_generation(
        self, session: Session, job_id: UUID, strategy: str
    ) -> tuple[UUID, str]:
        # check_execution holds the execution row lock for this transaction.
        checkpoint = session.execute(
            select(ingestion_job.c.checkpoint).where(ingestion_job.c.ingestion_run_id == job_id)
        ).scalar_one()
        generation_id = (
            UUID(checkpoint["generation_id"]) if "generation_id" in checkpoint else uuid4()
        )
        bound_strategy = checkpoint.get("chunking_strategy", strategy)
        session.execute(
            update(ingestion_job)
            .where(ingestion_job.c.ingestion_run_id == job_id)
            .values(
                checkpoint=ingestion_job.c.checkpoint.op("||")(
                    {"generation_id": str(generation_id), "chunking_strategy": bound_strategy}
                )
            )
        )
        return generation_id, bound_strategy

    def mark_chunking_model_started(self, session: Session, job_id: UUID, started: bool) -> None:
        checkpoint = (
            ingestion_job.c.checkpoint.op("||")({"chunking_model_started": True})
            if started
            else ingestion_job.c.checkpoint.op("-")("chunking_model_started")
        )
        session.execute(
            update(ingestion_job)
            .where(ingestion_job.c.ingestion_run_id == job_id)
            .values(checkpoint=checkpoint)
        )

    def advance_ingestion_stage(
        self,
        session: Session,
        job_id: UUID,
        expected_stage: IngestionStage,
        target_stage: IngestionStage,
    ) -> None:
        # The caller checks execution rights and holds the execution row lock.
        updated = session.execute(
            update(ingestion_job)
            .where(
                ingestion_job.c.ingestion_run_id == job_id,
                ingestion_job.c.stage == expected_stage,
            )
            .values(stage=target_stage)
            .returning(ingestion_job.c.ingestion_run_id)
        ).scalar_one_or_none()
        if updated is None:
            raise RuntimeError("ingestion stage changed before stage commit")

    def save_chunked_checkpoint(
        self, session: Session, job_id: UUID, chunk_ref: ArtifactRef
    ) -> None:
        session.execute(
            update(ingestion_job)
            .where(ingestion_job.c.ingestion_run_id == job_id)
            .values(
                stage=IngestionStage.EMBEDDING,
                checkpoint=ingestion_job.c.checkpoint.op("-")("chunking_model_started").op("||")(
                    {"chunk_batch_ref": chunk_ref.model_dump(mode="json")}
                ),
            )
        )

    def get_or_bind_index_profile(self, session: Session, job_id: UUID) -> str:
        # check_execution holds the execution row lock in this same transaction.
        profile_id = session.execute(
            select(ingestion_job.c.checkpoint["index_profile_id"].astext).where(
                ingestion_job.c.ingestion_run_id == job_id
            )
        ).scalar_one()
        if profile_id is not None:
            return profile_id
        profile_id = session.execute(
            select(knowledge_base.c.active_index_profile_id)
            .select_from(ingestion_job.join(document_version).join(document).join(knowledge_base))
            .where(ingestion_job.c.ingestion_run_id == job_id)
        ).scalar_one()
        session.execute(
            update(ingestion_job)
            .where(ingestion_job.c.ingestion_run_id == job_id)
            .values(
                checkpoint=ingestion_job.c.checkpoint.op("||")({"index_profile_id": profile_id})
            )
        )
        return profile_id

    def load_indexing_source_metadata(
        self, session: Session, job_id: UUID
    ) -> tuple[Document, PolicyMetadata]:
        row = (
            session.execute(
                select(document, document_version.c.policy_metadata)
                .select_from(ingestion_job.join(document_version).join(document))
                .where(ingestion_job.c.ingestion_run_id == job_id)
            )
            .mappings()
            .one()
        )
        return (
            Document.model_validate({column.name: row[column.name] for column in document.c}),
            PolicyMetadata.model_validate(row["policy_metadata"]),
        )
