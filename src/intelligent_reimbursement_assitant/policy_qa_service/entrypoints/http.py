from typing import Annotated, cast
from uuid import UUID

from fastapi import FastAPI, Header, Query, Request
from starlette.responses import JSONResponse, StreamingResponse

from intelligent_reimbursement_assitant.errors.http import business_error_response
from intelligent_reimbursement_assitant.execution_state_service.adapters.events import (
    SQLAlchemyEventRepository,
)
from intelligent_reimbursement_assitant.execution_state_service.entrypoints.sse import stream_events
from intelligent_reimbursement_assitant.policy_qa_service.application.service import PolicyQAService
from intelligent_reimbursement_assitant.policy_qa_service.domain.errors import QAFailure
from intelligent_reimbursement_assitant.policy_qa_service.domain.models import (
    AnswerResult,
    ConversationHistoryPage,
    QAJobView,
    QAMessage,
)
from intelligent_reimbursement_assitant.policy_qa_service.entrypoints.models import (
    ClarificationReplyCreate,
    ConversationCreated,
    QAAccepted,
    QuestionCreate,
)


def install_policy_qa(
    app: FastAPI, service: PolicyQAService, events: SQLAlchemyEventRepository
) -> None:
    async def handle_failure(request: Request, exception: Exception):
        return business_error_response(cast(QAFailure, exception).error)

    app.add_exception_handler(QAFailure, handle_failure)

    @app.get("/qa-runs/{job_id}")
    def get_run(job_id: UUID) -> QAJobView:
        return service.get_run(job_id)

    @app.post("/qa-runs/{job_id}/cancel")
    def cancel_run(job_id: UUID) -> QAJobView:
        return service.cancel_run(job_id)

    @app.get("/qa-runs/{job_id}/clarification")
    def get_prompt(job_id: UUID) -> QAMessage:
        return service.load_waiting_prompt(job_id)

    @app.post("/qa-runs/{job_id}/clarifications/{prompt_message_id}/replies", status_code=202)
    def reply(job_id: UUID, prompt_message_id: UUID, body: ClarificationReplyCreate) -> QAAccepted:
        return QAAccepted(
            job_id=service.accept_clarification_reply(
                job_id, prompt_message_id, body.message_id, body.text
            ).qa_run_id
        )

    @app.get(
        "/qa-runs/{job_id}/answer",
        response_model=AnswerResult,
        responses={202: {"model": QAAccepted}},
    )
    def get_answer(job_id: UUID) -> AnswerResult | JSONResponse:
        result = service.get_answer(job_id)
        return (
            result
            if result is not None
            else JSONResponse(QAAccepted(job_id=job_id).model_dump(mode="json"), status_code=202)
        )

    @app.get("/conversations/{conversation_id}/messages")
    def messages(
        conversation_id: UUID,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        before: UUID | None = None,
    ) -> ConversationHistoryPage:
        return service.list_messages(conversation_id, limit, before)

    @app.get("/qa-runs/{job_id}/events", response_model=None)
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
            stage="qa",
        )

    @app.post("/conversations", status_code=201, response_model=ConversationCreated)
    def create_conversation() -> ConversationCreated | JSONResponse:
        try:
            return ConversationCreated(conversation_id=service.create_conversation())
        except QAFailure as exc:
            return business_error_response(exc.error)

    @app.post(
        "/conversations/{conversation_id}/questions", status_code=202, response_model=QAAccepted
    )
    def accept_question(conversation_id: UUID, body: QuestionCreate) -> QAAccepted | JSONResponse:
        try:
            turn = service.accept_question(
                conversation_id, body.message_id, body.kb_id, body.question
            )
            return QAAccepted(job_id=turn.qa_run_id)
        except QAFailure as exc:
            return business_error_response(exc.error)
