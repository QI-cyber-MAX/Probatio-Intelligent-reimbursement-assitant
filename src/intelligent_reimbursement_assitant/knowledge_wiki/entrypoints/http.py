from typing import cast
from uuid import UUID

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError

from intelligent_reimbursement_assitant.errors.http import business_error_response
from intelligent_reimbursement_assitant.knowledge_wiki.application.service import WikiService
from intelligent_reimbursement_assitant.knowledge_wiki.domain.errors import (
    WikiEditValidationError,
    WikiFailure,
)
from intelligent_reimbursement_assitant.knowledge_wiki.domain.models import (
    WikiChunkOption,
    WikiJobView,
    WikiWorkspaceView,
)
from intelligent_reimbursement_assitant.knowledge_wiki.entrypoints.models import (
    WikiEditAccepted,
    WikiEditCreate,
)


def install_wiki(app: FastAPI, service: WikiService) -> None:
    async def handle_failure(request: Request, error: Exception):
        return business_error_response(cast(WikiFailure, error).error)

    app.add_exception_handler(WikiFailure, handle_failure)

    @app.get("/knowledge-bases/{kb_id}/wiki")
    def workspace(kb_id: UUID) -> WikiWorkspaceView:
        return service.get_workspace(kb_id)

    @app.get("/knowledge-bases/{kb_id}/wiki/source-pages/{page_id}/chunks")
    def chunks(kb_id: UUID, page_id: str, version_job_id: UUID) -> list[WikiChunkOption]:
        return service.get_source_chunks(kb_id, page_id, version_job_id)

    @app.post("/knowledge-bases/{kb_id}/wiki/versions", status_code=202)
    def submit(kb_id: UUID, body: WikiEditCreate) -> WikiEditAccepted:
        try:
            job_id = service.submit_wiki_edit(
                kb_id,
                body.new_job_id,
                body.base_job_id,
                body.source_pages,
                body.scenes,
                body.overview,
            )
        except WikiEditValidationError as error:
            raise RequestValidationError(error.errors) from error
        return WikiEditAccepted(job_id=job_id)

    @app.get("/wiki-jobs/{job_id}")
    def job(job_id: UUID) -> WikiJobView:
        return service.get_job(job_id)
