"""Document HTTP routes; registration follows multipart parsing and file scanning."""

import asyncio
import re
from contextlib import AbstractContextManager
from typing import Annotated, cast
from urllib.parse import quote
from uuid import UUID

from fastapi import FastAPI, Header, Path, Query, Request
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, StreamingResponse
from starlette.types import Receive, Scope, Send

from intelligent_reimbursement_assitant.contracts.models import SourceReference
from intelligent_reimbursement_assitant.document_ingestion_service.application.service import (
    DocumentIngestionService,
)
from intelligent_reimbursement_assitant.document_ingestion_service.domain.errors import (
    RegistrationFailure,
)
from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    DocumentRegistration,
    DocumentView,
    IngestionAccepted,
    KnowledgeBase,
    SourceMetadataUpdate,
)
from intelligent_reimbursement_assitant.document_ingestion_service.entrypoints.models import (
    IngestionRebuildCreate,
    KnowledgeBaseCreate,
)
from intelligent_reimbursement_assitant.document_ingestion_service.ports.repository import (
    DocumentItem,
    KnowledgeBaseItem,
)
from intelligent_reimbursement_assitant.errors.codes import ErrorCode as Code
from intelligent_reimbursement_assitant.errors.http import business_error_response
from intelligent_reimbursement_assitant.execution_state_service.adapters.events import (
    SQLAlchemyEventRepository,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    IngestionJobView,
)
from intelligent_reimbursement_assitant.execution_state_service.entrypoints.sse import stream_events
from intelligent_reimbursement_assitant.scanner.application.upload_intake import UploadIntake
from intelligent_reimbursement_assitant.scanner.domain.models import UploadPolicy
from intelligent_reimbursement_assitant.scanner.entrypoints.upload_input import (
    install_upload_input,
    parsed_upload_input,
    scanned_upload_input,
)

UPLOAD_PATH = "/knowledge-bases/{kb_id}/documents"
_FIELDS = {"version_status", "issuer", "effective_from", "effective_to", "document_id", "title"}


def install_document_ingestion(
    app: FastAPI,
    *,
    intake: UploadIntake,
    service: DocumentIngestionService,
    events: SQLAlchemyEventRepository,
    policy: UploadPolicy,
    max_concurrent_uploads: int = 2,
    slot_wait_seconds: float = 2,
    reception_timeout_seconds: float = 120,
) -> None:
    install_upload_input(
        app,
        paths={UPLOAD_PATH},
        policy=policy,
        max_concurrent_uploads=max_concurrent_uploads,
        slot_wait_seconds=slot_wait_seconds,
        reception_timeout_seconds=reception_timeout_seconds,
    )

    async def handle_failure(request: Request, exception: Exception):
        failure = cast(RegistrationFailure, exception)
        return business_error_response(failure.error)

    app.add_exception_handler(RegistrationFailure, handle_failure)

    @app.get("/knowledge-bases")
    def list_knowledge_bases() -> list[KnowledgeBaseItem]:
        return service.list_knowledge_bases()

    @app.post("/knowledge-bases", status_code=201)
    def create_knowledge_base(body: KnowledgeBaseCreate) -> KnowledgeBase:
        return service.create_knowledge_base(body.name)

    @app.get("/documents/{document_id}")
    def get_document(document_id: UUID) -> DocumentView:
        return service.get_document_view(document_id)

    @app.patch("/documents/{document_id}/sources/{source_hash}/metadata", status_code=202)
    async def update_metadata(
        document_id: UUID,
        source_hash: Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")],
        request: Request,
    ) -> IngestionAccepted:
        try:
            body = SourceMetadataUpdate.model_validate(await request.json())
        except (ValueError, ValidationError) as exc:
            raise RegistrationFailure(
                Code.INVALID_REGISTRATION, "版本或日期修改字段无效。"
            ) from exc
        return await run_in_threadpool(
            service.update_source_metadata,
            SourceReference(document_id=document_id, source_hash=source_hash),
            body,
        )

    @app.get("/documents/{document_id}/sources/{source_hash}/file")
    async def source_file(
        document_id: UUID,
        source_hash: Annotated[str, Path(pattern=r"^[0-9a-f]{64}$")],
        download: bool = False,
    ) -> StreamingResponse:
        manager = service.open_source_file(
            SourceReference(document_id=document_id, source_hash=source_hash)
        )
        title, ref, file = await run_in_threadpool(manager.__enter__)
        suffix = {
            "application/pdf": "pdf",
            "application/msword": "doc",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
        }[ref.media_type]
        filename = re.sub(r"[\\/\x00-\x1f\x7f]", "_", title)[:150] + f"-{source_hash[:12]}.{suffix}"
        disposition = "inline" if suffix == "pdf" and not download else "attachment"

        def chunks():
            while chunk := file.read(65536):
                yield chunk

        return _OriginalResponse(
            chunks(),
            manager=manager,
            media_type=ref.media_type,
            headers={
                "Content-Disposition": f"{disposition}; filename=\"document-{source_hash[:12]}.{suffix}\"; filename*=UTF-8''{quote(filename, safe='')}",
            },
        )

    @app.get(UPLOAD_PATH)
    def list_documents(
        kb_id: UUID,
        issuer: Annotated[str | None, Query(max_length=1024)] = None,
        title: Annotated[str | None, Query(max_length=1024)] = None,
    ) -> list[DocumentItem]:
        return service.list_documents(
            kb_id,
            issuer.strip() if issuer is not None else None,
            title.strip() if title is not None else None,
        )

    @app.get("/ingestion-jobs/{job_id}")
    def get_ingestion_job(job_id: UUID) -> IngestionJobView:
        return service.get_ingestion_job(job_id)

    @app.post("/ingestion-jobs/{job_id}/retry", status_code=202)
    def rebuild(job_id: UUID, body: IngestionRebuildCreate) -> IngestionAccepted:
        return service.rebuild_ingestion(job_id, body.new_job_id)

    @app.post("/ingestion-jobs/{job_id}/cancel")
    def cancel(job_id: UUID) -> IngestionJobView:
        return service.cancel_ingestion(job_id)

    @app.get("/ingestion-jobs/{job_id}/events", response_model=None)
    async def subscribe(
        job_id: UUID,
        after: Annotated[int, Query(ge=0)] = 0,
        last_event_id: Annotated[int | None, Header(ge=0)] = None,
    ) -> StreamingResponse | JSONResponse:
        return await stream_events(
            job_id,
            after if last_event_id is None else last_event_id,
            repository=events,
            read_head=service.read_event_head,
            stage="document_registration",
        )

    @app.post(UPLOAD_PATH, status_code=202)
    async def upload_document(request: Request, kb_id: UUID) -> IngestionAccepted:
        await asyncio.to_thread(service.require_knowledge_base, kb_id)
        async with parsed_upload_input(request, text_fields=_FIELDS) as (file, fields):
            # Optional empty HTML inputs are absent values; required/identity text stays strict.
            for name in ("effective_from", "effective_to", "document_id"):
                if fields.get(name) == "":
                    fields.pop(name)
            try:
                registration = DocumentRegistration.model_validate({"kb_id": kb_id, **fields})
            except ValidationError as exc:
                raise RegistrationFailure(
                    Code.INVALID_REGISTRATION, "登记字段无效，请核对制度、单位和日期。"
                ) from exc
            async with scanned_upload_input(request, intake, file) as (upload, report):
                worker = asyncio.create_task(
                    asyncio.to_thread(service.register_document, upload, report, registration)
                )
                try:
                    result = await asyncio.shield(worker)
                except asyncio.CancelledError:
                    # Cancellation of the HTTP task does not cancel a thread's COMMIT.
                    while not worker.done():
                        try:
                            await asyncio.shield(worker)
                        except asyncio.CancelledError:
                            continue
                        except Exception:
                            break
                    if worker.done() and not worker.cancelled():
                        worker.exception()
                    raise
        return result


class _OriginalResponse(StreamingResponse):
    def __init__(self, content, *, manager: AbstractContextManager, **kwargs):
        super().__init__(content, **kwargs)
        self.manager = manager

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            await run_in_threadpool(self.manager.__exit__, None, None, None)
