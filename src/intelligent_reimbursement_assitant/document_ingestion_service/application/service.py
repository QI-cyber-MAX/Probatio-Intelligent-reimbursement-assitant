"""File-first registration, with one short PG transaction and conservative recovery."""

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import BinaryIO, cast
from uuid import UUID, uuid4

from psycopg.errors import UniqueViolation
from pydantic import JsonValue
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from intelligent_reimbursement_assitant.contracts.models import ArtifactRef, SourceReference
from intelligent_reimbursement_assitant.document_ingestion_service.domain.errors import (
    RegistrationFailure,
)
from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    AcceptedUpload,
    Document,
    DocumentRegistration,
    DocumentView,
    IngestionAccepted,
    KnowledgeBase,
    PolicyMetadata,
    SourceMetadataUpdate,
)
from intelligent_reimbursement_assitant.document_ingestion_service.domain.policy_metadata import (
    resolve_policy_metadata,
    user_policy_date,
)
from intelligent_reimbursement_assitant.document_ingestion_service.ports.repository import (
    DocumentItem,
    DocumentRepository,
    KnowledgeBaseItem,
)
from intelligent_reimbursement_assitant.errors.codes import ErrorCode as Code
from intelligent_reimbursement_assitant.errors.execution import public_terminal_reason
from intelligent_reimbursement_assitant.execution_state_service.application.service import (
    ExecutionStateService,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionKind,
    ExecutionSettings,
    ExecutionStatus,
    IngestionJobView,
    IngestionStage,
)
from intelligent_reimbursement_assitant.scanner.domain.errors import UploadFailure
from intelligent_reimbursement_assitant.scanner.domain.models import QuarantinedUpload, ScanReport
from intelligent_reimbursement_assitant.scanner.ports.artifact_store import ArtifactStorePort

logger = logging.getLogger(__name__)


def _unavailable() -> RegistrationFailure:
    return RegistrationFailure(
        Code.REGISTRATION_UNAVAILABLE, "登记服务暂不可用，请稍后重试。", retryable=True
    )


class DocumentIngestionService:
    def __init__(
        self,
        repository: DocumentRepository,
        store: ArtifactStorePort,
        state_service: ExecutionStateService,
        settings: ExecutionSettings,
        default_index_profile_id: str,
    ):
        self.repository = repository
        self.store = store
        self.state_service = state_service
        self.settings = settings
        self.default_index_profile_id = default_index_profile_id

    def create_knowledge_base(self, name: str) -> KnowledgeBase:
        value = KnowledgeBase(
            kb_id=uuid4(),
            name=name,
            revision=0,
            active_index_profile_id=self.default_index_profile_id,
            created_at=datetime.now(UTC),
        )
        commit_started = False
        try:
            with self.repository.session() as session:
                self.repository.create_knowledge_base(session, value)
                commit_started = True
                session.commit()
        except SQLAlchemyError as exc:
            if commit_started:
                raise RegistrationFailure(
                    Code.REGISTRATION_OUTCOME_UNKNOWN, "知识库创建结果尚未确认，请先查询。"
                ) from exc
            raise _unavailable() from exc
        return value

    def get_document_view(self, document_id: UUID) -> DocumentView:
        try:
            with self.repository.session() as session:
                value = self.repository.get_document_view(session, document_id)
        except SQLAlchemyError as exc:
            raise _unavailable() from exc
        if value is None:
            raise RegistrationFailure(Code.DOCUMENT_NOT_FOUND, "指定制度不存在。")
        return value.model_copy(
            update={
                "versions": [
                    source.model_copy(update={"latest_job": _public_job(source.latest_job)})
                    for source in value.versions
                ]
            }
        )

    @contextmanager
    def open_source_file(
        self, source: SourceReference
    ) -> Iterator[tuple[str, ArtifactRef, BinaryIO]]:
        try:
            with self.repository.session() as session:
                value = self.repository.get_source_file(session, source)
        except SQLAlchemyError as exc:
            raise _unavailable() from exc
        if value is None:
            raise RegistrationFailure(Code.DOCUMENT_SOURCE_NOT_FOUND, "指定原件不存在。")
        title, ref = value
        manager = self.store.open_artifact(ref)
        try:
            file = manager.__enter__()
        except (OSError, UploadFailure) as exc:
            raise RegistrationFailure(
                Code.ARTIFACT_READ_ERROR, "原件暂时无法读取，请联系维护人员。"
            ) from exc
        try:
            yield title, ref, file
        finally:
            manager.__exit__(None, None, None)

    def _operation_replay(
        self,
        session: Session,
        new_job_id: UUID,
        source: SourceReference,
        checkpoint: dict[str, JsonValue],
    ) -> IngestionAccepted | None:
        identity = self.repository.find_job_identity(session, new_job_id)
        if identity is None:
            return None
        if identity == (ExecutionKind.INGESTION, source):
            saved = self.repository.load_ingestion_source(session, new_job_id)["checkpoint"]
            if saved.get("metadata_update") == checkpoint.get("metadata_update"):
                return IngestionAccepted(
                    **source.model_dump(),
                    job_id=new_job_id,
                    status_url=f"/ingestion-jobs/{new_job_id}",
                )
        raise RegistrationFailure(Code.INGESTION_JOB_ID_CONFLICT, "任务 ID 已用于其他操作或输入。")

    def rebuild_ingestion(self, job_id: UUID, new_job_id: UUID) -> IngestionAccepted:
        try:
            with self.repository.session() as session:
                identity = self.repository.find_job_identity(session, job_id)
                if identity is None or identity[0] != ExecutionKind.INGESTION:
                    raise RegistrationFailure(Code.INGESTION_JOB_NOT_FOUND, "摄入任务不存在。")
                previous_source = self.repository.load_ingestion_source(session, job_id)
        except SQLAlchemyError as exc:
            raise _unavailable() from exc
        if new_job_id == job_id:
            raise RegistrationFailure(Code.INGESTION_JOB_ID_CONFLICT, "重建必须使用新的任务 ID。")
        checkpoint = (
            previous_source["checkpoint"]
            if "metadata_update" in previous_source["checkpoint"]
            else {}
        )
        return self._accept_source_operation(
            cast(SourceReference, identity[1]),
            new_job_id,
            checkpoint,
            previous_job_id=job_id,
        )

    def update_source_metadata(
        self,
        source: SourceReference,
        update: SourceMetadataUpdate,
    ) -> IngestionAccepted:
        return self._accept_source_operation(
            source,
            update.new_job_id,
            {
                "metadata_update": update.model_dump(
                    mode="json", exclude={"new_job_id"}, exclude_unset=True
                ),
            },
            update=update,
        )

    def _accept_source_operation(
        self,
        source: SourceReference,
        new_job_id: UUID,
        checkpoint: dict[str, JsonValue],
        *,
        previous_job_id: UUID | None = None,
        update: SourceMetadataUpdate | None = None,
    ) -> IngestionAccepted:
        commit_started = False
        try:
            with self.repository.session() as session:
                if not self.repository.lock_source(session, source):
                    raise RegistrationFailure(Code.DOCUMENT_SOURCE_NOT_FOUND, "指定原件不存在。")
                replay = self._operation_replay(session, new_job_id, source, checkpoint)
                if replay is not None:
                    return replay
                if previous_job_id is not None:
                    previous = cast(
                        IngestionJobView,
                        self.repository.get_ingestion_job(session, previous_job_id),
                    )
                    if previous.status not in {
                        ExecutionStatus.SUCCEEDED,
                        ExecutionStatus.FAILED,
                        ExecutionStatus.CANCELLED,
                    }:
                        raise RegistrationFailure(
                            Code.INGESTION_REBUILD_NOT_ALLOWED,
                            "任务尚未结束，请先取消或等待处理结束。",
                        )
                if self.repository.has_active_ingestion(session, source):
                    raise RegistrationFailure(
                        Code.INGESTION_SOURCE_BUSY,
                        "原件仍有活动任务，请等待结束后重试。",
                        retryable=True,
                    )
                if update is not None:
                    _, current, published = self.repository.load_publication_sources(
                        session, source
                    )
                    try:
                        resolve_policy_metadata(current, None, update)
                    except ValueError as exc:
                        raise RegistrationFailure(
                            Code.INVALID_REGISTRATION, "截止日期应晚于生效日期。"
                        ) from exc
                    entry = next((entry for entry in published if entry.source == source), None)
                    checkpoint = {
                        **checkpoint,
                        "target_generation_id": str(entry.generation_id) if entry else None,
                        "index_profile_id": entry.index_profile_id if entry else None,
                    }
                self.state_service.enqueue_initial(
                    session, self.settings, kind=ExecutionKind.INGESTION, job_id=new_job_id
                )
                self.repository.create_ingestion_job(
                    session,
                    new_job_id,
                    source.document_id,
                    source.source_hash,
                    checkpoint=checkpoint,
                    stage=IngestionStage.PUBLISHING
                    if "metadata_update" in checkpoint
                    else IngestionStage.REGISTERED,
                )
                commit_started = True
                session.commit()
                return IngestionAccepted(
                    **source.model_dump(),
                    job_id=new_job_id,
                    status_url=f"/ingestion-jobs/{new_job_id}",
                )
        except IntegrityError as exc:
            if not (
                isinstance(exc.orig, UniqueViolation)
                and exc.orig.diag.constraint_name == "execution_run_pkey"
            ):
                raise _unavailable() from exc
            try:
                with self.repository.session() as session:
                    replay = self._operation_replay(session, new_job_id, source, checkpoint)
                    if replay is not None:
                        return replay
            except SQLAlchemyError as read_exc:
                raise _unavailable() from read_exc
            raise _unavailable() from exc
        except SQLAlchemyError as exc:
            if not commit_started:
                raise _unavailable() from exc
            try:
                with self.repository.session() as session:
                    replay = self._operation_replay(session, new_job_id, source, checkpoint)
                    if replay is not None:
                        return replay
            except SQLAlchemyError:
                pass
            raise RegistrationFailure(
                Code.REGISTRATION_OUTCOME_UNKNOWN, "任务受理结果尚未确认，请保留本次任务 ID 查询。"
            ) from exc

    def cancel_ingestion(self, job_id: UUID) -> IngestionJobView:
        commit_started = False
        try:
            with self.repository.session() as session:
                if not self.state_service.request_cancel(
                    session, job_id, kind=ExecutionKind.INGESTION
                ):
                    raise RegistrationFailure(Code.INGESTION_JOB_NOT_FOUND, "摄入任务不存在。")
                result = cast(IngestionJobView, self.repository.get_ingestion_job(session, job_id))
                commit_started = True
                session.commit()
                return _public_job(result)
        except SQLAlchemyError as exc:
            if commit_started:
                raise RegistrationFailure(
                    Code.TASK_OPERATION_OUTCOME_UNKNOWN, "取消结果尚未确认，请查询本任务。"
                ) from exc
            raise _unavailable() from exc

    def read_event_head(self, job_id: UUID) -> tuple[IngestionJobView, int]:
        with self.repository.session() as session:
            result = self.repository.read_event_head(session, job_id)
        if result is None:
            raise RegistrationFailure(Code.INGESTION_JOB_NOT_FOUND, "摄入任务不存在。")
        return _public_job(result[0]), result[1]

    def require_knowledge_base(self, kb_id: UUID) -> None:
        try:
            with self.repository.session() as session:
                if not self.repository.knowledge_base_exists(session, kb_id):
                    raise RegistrationFailure(Code.KNOWLEDGE_BASE_NOT_FOUND, "指定知识库不存在。")
        except SQLAlchemyError as exc:
            raise _unavailable() from exc

    def register_document(
        self, upload: QuarantinedUpload, report: ScanReport, registration: DocumentRegistration
    ) -> IngestionAccepted:
        try:
            with self.repository.session() as session:
                found, accepted = self._resolve_document_identity(
                    registration, upload.sha256, session
                )
        except SQLAlchemyError as exc:
            raise _unavailable() from exc
        if accepted is not None:
            return accepted
        try:
            reference = self.store.promote(upload)
        except UploadFailure as exc:
            raise UploadFailure(
                exc.error.code,
                exc.error.user_message,
                retryable=exc.error.retryable,
                stage="document_registration",
                detail_ref=exc.error.detail_ref,
            ) from exc
        accepted_upload = AcceptedUpload(
            source_ref=reference,
            original_name=upload.original_name,
            detected_type=upload.detected_type,
            source_hash=upload.sha256,
            scan_report=report,
        )
        policy = PolicyMetadata(
            issuer=registration.issuer,
            version_status=registration.version_status,
            effective_from=user_policy_date(registration.effective_from),
            effective_to=user_policy_date(registration.effective_to),
        )
        commit_started = False
        rollback_unknown = False
        document_id: UUID | None = None
        adopted = False
        try:
            with self.repository.session() as session:
                try:
                    session.begin()
                    if found is None:
                        found = self.repository.get_or_create_document(session, registration)
                    document_id = found.document_id
                    adopted = self.repository.insert_source_if_absent(
                        session, document_id, accepted_upload, policy
                    )
                    if adopted:
                        job_id = self.state_service.enqueue_initial(
                            session, self.settings, kind=ExecutionKind.INGESTION
                        )
                        self.repository.create_ingestion_job(
                            session, job_id, document_id, upload.sha256
                        )
                        accepted = IngestionAccepted(
                            document_id=document_id,
                            source_hash=upload.sha256,
                            job_id=job_id,
                            status_url=f"/ingestion-jobs/{job_id}",
                        )
                    else:
                        accepted = self.repository.find_latest_ingestion_accepted(
                            session, document_id, upload.sha256
                        )
                    commit_started = True
                    session.commit()
                except BaseException:
                    if not commit_started:
                        # No COMMIT was sent. Roll back before releasing this attempt's file.
                        try:
                            session.rollback()
                        except BaseException:
                            rollback_unknown = True
                            raise
                    raise
        except BaseException as exc:
            if commit_started or rollback_unknown:
                # A failed COMMIT acknowledgement can follow a successful server commit.
                if document_id is not None:
                    recovered = self._reconcile(document_id, upload.sha256, reference.key)
                    if recovered is not None:
                        return recovered
                logger.error("registration_outcome_unknown key=%s", reference.key)
                raise RegistrationFailure(
                    Code.REGISTRATION_OUTCOME_UNKNOWN, "登记结果尚未确认，请稍后核对或重新上传。"
                ) from exc
            self._discard(reference.key)
            if isinstance(exc, SQLAlchemyError):
                raise _unavailable() from exc
            raise
        if not adopted:
            self._discard(reference.key)
        return cast(IngestionAccepted, accepted)

    def _resolve_document_identity(
        self, registration: DocumentRegistration, source_hash: str, session: Session
    ) -> tuple[Document | None, IngestionAccepted | None]:
        if registration.document_id is not None:
            found = self.repository.get_document(session, registration.document_id)
            if found is None:
                raise RegistrationFailure(Code.DOCUMENT_NOT_FOUND, "指定制度不存在。")
            if found.kb_id != registration.kb_id or found.issuer != registration.issuer:
                raise RegistrationFailure(
                    Code.DOCUMENT_OWNERSHIP_MISMATCH, "制度不属于指定知识库或发文单位。"
                )
        else:
            found = self.repository.find_document(session, registration)
        accepted = (
            self.repository.find_latest_ingestion_accepted(session, found.document_id, source_hash)
            if found is not None
            else None
        )
        return found, accepted

    def _reconcile(
        self, document_id: UUID, source_hash: str, artifact_key: str
    ) -> IngestionAccepted | None:
        try:
            with self.repository.session() as session:
                key = self.repository.find_source_key(session, document_id, source_hash)
                if key is None:
                    # Absence cannot prove the in-flight transaction rolled back.
                    return None
                accepted = self.repository.find_latest_ingestion_accepted(
                    session, document_id, source_hash
                )
        except SQLAlchemyError:
            return None
        if key != artifact_key:
            self._discard(artifact_key)
        return accepted

    def _discard(self, artifact_key: str) -> None:
        try:
            self.store.discard_artifact(artifact_key)
        except OSError, UploadFailure:
            logger.error("original_cleanup_failed key=%s", artifact_key)

    def list_knowledge_bases(self) -> list[KnowledgeBaseItem]:
        try:
            with self.repository.session() as session:
                return self.repository.list_knowledge_bases(session)
        except SQLAlchemyError as exc:
            raise _unavailable() from exc

    def cleanup_unreferenced_originals(self, *, cutoff_timestamp: float) -> list[str]:
        """Operator-only, with uploads stopped and all registration transactions settled.

        Do not use a snapshot while a COMMIT is still in flight. A failed PG read
        exits before filesystem cleanup, preserving originals for later recovery.
        """
        try:
            with self.repository.session() as session:
                referenced = self.repository.referenced_artifact_keys(session)
        except SQLAlchemyError as exc:
            raise _unavailable() from exc
        return self.store.cleanup_unreferenced_originals(
            cutoff_timestamp=cutoff_timestamp, referenced_keys=referenced
        )

    def list_documents(
        self, kb_id: UUID, issuer: str | None, title: str | None
    ) -> list[DocumentItem]:
        self.require_knowledge_base(kb_id)
        try:
            with self.repository.session() as session:
                return self.repository.list_documents(session, kb_id, issuer, title)
        except SQLAlchemyError as exc:
            raise _unavailable() from exc

    def get_ingestion_job(self, job_id: UUID) -> IngestionJobView:
        try:
            with self.repository.session() as session:
                result = self.repository.get_ingestion_job(session, job_id)
        except SQLAlchemyError as exc:
            raise _unavailable() from exc
        if result is None:
            raise RegistrationFailure(Code.INGESTION_JOB_NOT_FOUND, "摄入任务不存在。")
        return _public_job(result)


def _public_job(result: IngestionJobView) -> IngestionJobView:
    return result.model_copy(
        update={
            "terminal_reason": public_terminal_reason(
                result.last_error_code, result.terminal_reason
            )
        }
    )
