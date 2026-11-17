import logging
from collections.abc import Callable
from contextlib import contextmanager
from uuid import UUID

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from intelligent_reimbursement_assitant.contracts.models import ArtifactRef
from intelligent_reimbursement_assitant.document_ingestion_service.ports.repository import (
    DocumentRepository,
)
from intelligent_reimbursement_assitant.errors.codes import ErrorCode as Code
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.errors.execution import (
    exception_diagnostic,
    public_terminal_reason,
)
from intelligent_reimbursement_assitant.execution_state_service.application.service import (
    ExecutionStateService,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionKind,
    ExecutionSettings,
    ExecutionStatus,
)
from intelligent_reimbursement_assitant.knowledge_wiki.domain.compilation import document_page_id
from intelligent_reimbursement_assitant.knowledge_wiki.domain.editing import build_edited_pages
from intelligent_reimbursement_assitant.knowledge_wiki.domain.errors import WikiFailure
from intelligent_reimbursement_assitant.knowledge_wiki.domain.models import (
    WikiBuildInput,
    WikiChunkOption,
    WikiJobSummary,
    WikiJobView,
    WikiOverviewEdit,
    WikiPackage,
    WikiSceneEdit,
    WikiSourcePageEdit,
    WikiVersionView,
    WikiWorkspaceView,
)
from intelligent_reimbursement_assitant.knowledge_wiki.ports.artifacts import WikiBuildReaderPort
from intelligent_reimbursement_assitant.knowledge_wiki.ports.repository import (
    WikiBuild,
    WikiRepository,
)


def enqueue_wiki_build(
    repository: WikiRepository,
    documents: DocumentRepository,
    state: ExecutionStateService,
    settings: ExecutionSettings,
    kb_id: UUID,
    *,
    rebuild: bool = False,
) -> tuple[UUID | None, str]:
    with repository.session() as session:
        repository.lock_knowledge_base(session, kb_id)
        active = repository.find_active_build(session, kb_id)
        if active is not None:
            return active["job_id"], "active"
        snapshot = documents.load_knowledge_snapshot(session, kb_id)
        if snapshot is None or not snapshot.entries:
            return None, "no_published_sources"
        if (
            not rebuild
            and repository.find_published_ref(session, kb_id, snapshot.revision) is not None
        ):
            return None, "unchanged"
        latest = repository.load_latest_build(session, kb_id, automatic=True)
        if (
            not rebuild
            and latest is not None
            and latest["input"].snapshot.revision == snapshot.revision
            and latest["status"] in {ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}
        ):
            return latest["job_id"], "requires_attention"
        base = repository.load_published_build(session, kb_id)
        base_ref = (
            ArtifactRef.model_validate(base["checkpoint"]["result_ref"])
            if base is not None
            else None
        )
        value = WikiBuildInput(
            snapshot=snapshot,
            artifacts=documents.load_published_artifacts(session, snapshot),
            base_ref=base_ref,
            rebuild_sources=rebuild,
        )
        job_id = state.enqueue_initial(session, settings, kind=ExecutionKind.WIKI_BUILD)
        repository.create_build(session, job_id, value)
        session.commit()
        return job_id, "queued"


def discover_wiki_updates(
    repository: WikiRepository,
    documents: DocumentRepository,
    state: ExecutionStateService,
    settings: ExecutionSettings,
    *,
    batch_size: int,
    stopping: Callable[[], bool],
) -> None:
    after = None
    while not stopping():
        with repository.session() as session:
            ids = repository.list_update_candidates(session, after, batch_size)
        if not ids:
            return
        for kb_id in ids:
            if stopping():
                return
            try:
                enqueue_wiki_build(repository, documents, state, settings, kb_id)
            except ExecutionError as error:
                logging.getLogger(__name__).error(
                    "Wiki discovery source unavailable kb_id=%s code=%s %s",
                    kb_id,
                    error.code,
                    exception_diagnostic(error),
                )
        after = ids[-1]


class WikiService:
    """Read immutable versions and admit validated human publication tasks."""

    def __init__(
        self,
        repository: WikiRepository,
        documents: DocumentRepository,
        state: ExecutionStateService,
        settings: ExecutionSettings,
        reader: WikiBuildReaderPort,
    ):
        self.repository, self.documents, self.state = repository, documents, state
        self.settings, self.reader = settings, reader

    @contextmanager
    def boundary(self):
        try:
            yield
        except SQLAlchemyError as error:
            raise WikiFailure(Code.WIKI_UNAVAILABLE, "知识导航暂不可用", retryable=True) from error
        except ExecutionError as error:
            raise WikiFailure(
                Code.WIKI_UNAVAILABLE, "知识导航产物暂不可读", retryable=True
            ) from error

    def require_kb(self, session, kb_id: UUID) -> None:
        if not self.documents.knowledge_base_exists(session, kb_id):
            raise WikiFailure(Code.KNOWLEDGE_BASE_NOT_FOUND, "知识库不存在")

    def version(self, session, kb_id: UUID, job_id: UUID) -> WikiBuild:
        build = self.repository.find_build(session, job_id)
        if build is None or build["kb_id"] != kb_id or not build["checkpoint"].get("result_ref"):
            raise WikiFailure(Code.WIKI_NOT_FOUND, "知识导航版本不存在")
        return build

    def read_package(self, build: WikiBuild) -> WikiPackage:
        return self.reader.read_package(
            ArtifactRef.model_validate(build["checkpoint"]["result_ref"]), build["kb_id"]
        )

    def get_workspace(self, kb_id: UUID) -> WikiWorkspaceView:
        with self.boundary():
            with self.repository.session() as session:
                self.require_kb(session, kb_id)
                _, revision = self.repository.lock_knowledge_base(session, kb_id)
                published = self.repository.load_published_build(session, kb_id)
                active = self.repository.find_active_build(session, kb_id)
            package = self.read_package(published) if published is not None else None
            return WikiWorkspaceView(
                kb_id=kb_id,
                current_revision=revision,
                published_version=WikiVersionView(
                    job_id=package.job_id,
                    snapshot_revision=package.snapshot.revision,
                    pages=package.pages,
                )
                if package is not None
                else None,
                active_job=WikiJobSummary(
                    job_id=active["job_id"],
                    status=active["status"],
                    operation="edit" if active["input"].edited_pages is not None else "build",
                )
                if active is not None
                else None,
            )

    def get_job(self, job_id: UUID) -> WikiJobView:
        with self.boundary():
            with self.repository.session() as session:
                build = self.repository.find_build(session, job_id)
                if build is None:
                    raise WikiFailure(Code.WIKI_JOB_NOT_FOUND, "知识导航任务不存在")
                published = self.repository.load_published_build(session, build["kb_id"])
            return WikiJobView(
                job_id=job_id,
                kb_id=build["kb_id"],
                status=build["status"],
                operation="edit" if build["input"].edited_pages is not None else "build",
                next_retry_at=build["next_retry_at"],
                last_error_code=build["last_error_code"],
                terminal_reason=public_terminal_reason(
                    build["last_error_code"], build["terminal_reason"]
                ),
                result_version_job_id=job_id if build["checkpoint"].get("result_ref") else None,
                is_current=published is not None and published["job_id"] == job_id,
            )

    def get_source_chunks(
        self, kb_id: UUID, page_id: str, version_job_id: UUID
    ) -> list[WikiChunkOption]:
        with self.boundary():
            with self.repository.session() as session:
                self.require_kb(session, kb_id)
                build = self.version(session, kb_id, version_job_id)
            package = self.read_package(build)
            entry = next(
                (e for e in package.snapshot.entries if document_page_id(e) == page_id), None
            )
            if entry is None:
                raise WikiFailure(Code.WIKI_PAGE_NOT_FOUND, "来源页不存在")
            batch = self.reader.read_chunks(entry, package.artifacts[entry.generation_id])
            return [
                WikiChunkOption(
                    **chunk.model_dump(
                        include={"generation_id", "chunk_id", "ordinal", "body_text", "locators"}
                    )
                )
                for chunk in sorted(batch.chunks, key=lambda c: c.ordinal)
            ]

    def submit_wiki_edit(
        self,
        kb_id: UUID,
        new_job_id: UUID,
        base_job_id: UUID,
        source_pages: list[WikiSourcePageEdit],
        scenes: list[WikiSceneEdit],
        overview: WikiOverviewEdit,
    ) -> UUID:
        with self.boundary():
            with self.repository.session() as session:
                self.require_kb(session, kb_id)
                base = self.version(session, kb_id, base_job_id)
            package = self.read_package(base)
            valid_chunks = {
                document_page_id(e): {
                    (c.generation_id, c.chunk_id)
                    for c in self.reader.read_chunks(e, package.artifacts[e.generation_id]).chunks
                }
                for e in package.snapshot.entries
            }
            pages = build_edited_pages(
                package, new_job_id, source_pages, scenes, overview, valid_chunks
            )
            value = WikiBuildInput(
                snapshot=package.snapshot,
                artifacts=package.artifacts,
                base_ref=ArtifactRef.model_validate(base["checkpoint"]["result_ref"]),
                edited_pages=pages,
            )
            with self.repository.session() as session:
                _, revision = self.repository.lock_knowledge_base(session, kb_id)
                existing = self.repository.find_build(session, new_job_id)
                if (
                    existing is not None
                    and existing["kb_id"] == kb_id
                    and existing["input"] == value
                ):
                    return new_job_id
                if self.repository.job_id_in_use(session, new_job_id):
                    raise WikiFailure(Code.WIKI_EDIT_ID_CONFLICT, "提交标识已用于其他请求")
                active = self.repository.find_active_build(session, kb_id)
                if active is not None:
                    edit = active["input"].edited_pages is not None
                    raise WikiFailure(
                        Code.WIKI_EDIT_IN_PROGRESS if edit else Code.WIKI_BUILD_IN_PROGRESS,
                        "知识导航正在发布" if edit else "知识导航正在自动构建",
                    )
                published = self.repository.load_published_build(session, kb_id)
                if published is None or published["job_id"] != base_job_id:
                    raise WikiFailure(Code.WIKI_VERSION_CONFLICT, "导航版本已更新，请重新读取")
                if revision != package.snapshot.revision:
                    raise WikiFailure(
                        Code.WIKI_SOURCE_REVISION_CONFLICT, "来源已更新，请先完成导航构建"
                    )
                try:
                    self.state.enqueue_initial(
                        session, self.settings, kind=ExecutionKind.WIKI_BUILD, job_id=new_job_id
                    )
                    self.repository.create_build(session, new_job_id, value)
                except IntegrityError as error:
                    raise WikiFailure(
                        Code.WIKI_EDIT_ID_CONFLICT, "提交标识已用于其他请求"
                    ) from error
                try:
                    session.commit()
                except SQLAlchemyError as error:
                    raise WikiFailure(
                        Code.WIKI_EDIT_OUTCOME_UNKNOWN,
                        "发布受理结果未知，请使用同一提交标识核对任务",
                    ) from error
                return new_job_id
