from uuid import UUID

from pydantic import JsonValue
from sqlalchemy import Engine, func, insert, select, update
from sqlalchemy.orm import Session, sessionmaker

from intelligent_reimbursement_assitant.contracts.models import ArtifactRef
from intelligent_reimbursement_assitant.document_ingestion_service.adapters.schema import (
    knowledge_base,
)
from intelligent_reimbursement_assitant.execution_state_service.adapters.schema import execution_run
from intelligent_reimbursement_assitant.execution_state_service.application.service import TERMINAL
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionContext,
    ExecutionStatus,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.ownership import (
    require_execution_owner,
)
from intelligent_reimbursement_assitant.knowledge_wiki.adapters.schema import wiki_build
from intelligent_reimbursement_assitant.knowledge_wiki.domain.models import WikiBuildInput
from intelligent_reimbursement_assitant.knowledge_wiki.ports.repository import WikiBuild


class SQLAlchemyWikiRepository:
    def __init__(self, engine: Engine):
        self.sessions = sessionmaker(engine.execution_options(isolation_level="READ COMMITTED"))

    def session(self) -> Session:
        return self.sessions()

    def list_update_candidates(
        self, session: Session, after: UUID | None, limit: int
    ) -> list[UUID]:
        statement = select(knowledge_base.c.kb_id).order_by(knowledge_base.c.kb_id).limit(limit)
        if after is not None:
            statement = statement.where(knowledge_base.c.kb_id > after)
        return list(session.scalars(statement))

    def lock_knowledge_base(self, session: Session, kb_id: UUID) -> tuple[str, int]:
        row = session.execute(
            select(knowledge_base.c.name, knowledge_base.c.revision)
            .where(knowledge_base.c.kb_id == kb_id)
            .with_for_update()
        ).one()
        return row.name, row.revision

    def _read(self, session: Session, statement) -> WikiBuild | None:
        row = session.execute(statement).mappings().one_or_none()
        if row is None:
            return None
        return WikiBuild(
            job_id=row["job_id"],
            kb_id=row["kb_id"],
            input=WikiBuildInput.model_validate(row["input"]),
            checkpoint=row["checkpoint"],
            status=ExecutionStatus(row["status"]),
            next_retry_at=row["next_retry_at"],
            last_error_code=row["last_error_code"],
            terminal_reason=row["terminal_reason"],
        )

    def _select(self):
        return select(
            wiki_build,
            execution_run.c.status,
            execution_run.c.next_retry_at,
            execution_run.c.last_error_code,
            execution_run.c.terminal_reason,
        ).select_from(wiki_build.join(execution_run))

    def find_active_build(self, session: Session, kb_id: UUID) -> WikiBuild | None:
        return self._read(
            session,
            self._select()
            .where(wiki_build.c.kb_id == kb_id, execution_run.c.status.not_in(TERMINAL))
            .limit(1),
        )

    def load_latest_build(
        self, session: Session, kb_id: UUID, *, successful: bool = False, automatic: bool = False
    ) -> WikiBuild | None:
        statement = self._select().where(wiki_build.c.kb_id == kb_id)
        if automatic:
            statement = statement.where(wiki_build.c.input["edited_pages"].as_string().is_(None))
        if successful:
            statement = statement.where(
                execution_run.c.status == ExecutionStatus.SUCCEEDED,
                wiki_build.c.checkpoint["result_ref"].is_not(None),
            )
        return self._read(
            session,
            statement.order_by(execution_run.c.created_at.desc(), wiki_build.c.job_id.desc()).limit(
                1
            ),
        )

    def create_build(self, session: Session, job_id: UUID, value: WikiBuildInput) -> None:
        session.execute(
            insert(wiki_build).values(
                job_id=job_id,
                kb_id=value.snapshot.kb_id,
                input=value.model_dump(mode="json"),
                checkpoint={},
            )
        )

    def load_build(self, session: Session, job_id: UUID) -> WikiBuild:
        result = self.find_build(session, job_id)
        if result is None:
            raise LookupError("Wiki build does not exist")
        return result

    def find_build(self, session: Session, job_id: UUID) -> WikiBuild | None:
        return self._read(session, self._select().where(wiki_build.c.job_id == job_id))

    def job_id_in_use(self, session: Session, job_id: UUID) -> bool:
        return (
            session.scalar(select(execution_run.c.job_id).where(execution_run.c.job_id == job_id))
            is not None
        )

    def save_checkpoint(
        self, session: Session, ctx: ExecutionContext, checkpoint: dict[str, JsonValue]
    ) -> None:
        run = (
            session.execute(
                select(execution_run).where(execution_run.c.job_id == ctx.job_id).with_for_update()
            )
            .mappings()
            .one()
        )
        require_execution_owner(
            run, ctx, session.execute(select(func.clock_timestamp())).scalar_one()
        )
        session.execute(
            update(wiki_build)
            .where(wiki_build.c.job_id == ctx.job_id)
            .values(checkpoint=checkpoint)
        )

    def load_published_build(self, session: Session, kb_id: UUID) -> WikiBuild | None:
        return self._read(
            session,
            self._select()
            .join(knowledge_base, knowledge_base.c.published_wiki_job_id == wiki_build.c.job_id)
            .where(knowledge_base.c.kb_id == kb_id),
        )

    def find_published_ref(
        self, session: Session, kb_id: UUID, revision: int
    ) -> ArtifactRef | None:
        row = session.execute(
            select(wiki_build.c.checkpoint["result_ref"])
            .select_from(
                knowledge_base.join(
                    wiki_build, knowledge_base.c.published_wiki_job_id == wiki_build.c.job_id
                )
            )
            .where(
                knowledge_base.c.kb_id == kb_id,
                wiki_build.c.kb_id == kb_id,
                wiki_build.c.input["snapshot"]["revision"].as_integer() == revision,
            )
        ).one_or_none()
        return (
            ArtifactRef.model_validate(row[0]) if row is not None and row[0] is not None else None
        )

    def set_published_build(self, session: Session, kb_id: UUID, job_id: UUID) -> None:
        session.execute(
            update(knowledge_base)
            .where(knowledge_base.c.kb_id == kb_id)
            .values(published_wiki_job_id=job_id)
        )
