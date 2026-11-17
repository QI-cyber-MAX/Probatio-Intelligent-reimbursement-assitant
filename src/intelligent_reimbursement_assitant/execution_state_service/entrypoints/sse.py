"""Replay committed events, publish a consistent snapshot, then follow the same Job."""

import asyncio
import json
import time
from collections.abc import Callable
from typing import Protocol
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse, StreamingResponse

from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import BusinessError
from intelligent_reimbursement_assitant.errors.http import business_error_response
from intelligent_reimbursement_assitant.execution_state_service.adapters.events import (
    SQLAlchemyEventRepository,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.events import ExecutionEvent
from intelligent_reimbursement_assitant.execution_state_service.domain.models import ExecutionStatus


class JobSnapshot(Protocol):
    @property
    def status(self) -> ExecutionStatus: ...
    def model_dump_json(self) -> str: ...


def _stream_error(stage: str) -> BusinessError[ErrorCode]:
    return BusinessError[ErrorCode](
        code=ErrorCode.STREAM_UNAVAILABLE,
        stage=stage,
        retryable=True,
        user_message="事件流暂不可用，请保留最近游标重新连接。",
    )


async def stream_events(
    job_id: UUID,
    cursor: int,
    *,
    repository: SQLAlchemyEventRepository,
    read_head: Callable[[UUID], tuple[JobSnapshot, int]],
    stage: str,
) -> StreamingResponse | JSONResponse:
    try:
        snapshot, high = await run_in_threadpool(read_head, job_id)
    except SQLAlchemyError:
        return business_error_response(_stream_error(stage))
    if cursor > high:
        return business_error_response(
            BusinessError[ErrorCode](
                code=ErrorCode.SSE_CURSOR_INVALID,
                stage=stage,
                retryable=False,
                user_message="事件游标超过当前任务的事件范围。",
            )
        )

    async def generate():
        nonlocal cursor, snapshot, high
        heartbeat_at = time.monotonic()
        try:
            while cursor < high:
                events = await run_in_threadpool(repository.read_events, job_id, cursor, high)
                for event in events:
                    cursor = event.sequence
                    yield _encode(event)
                    heartbeat_at = time.monotonic()
            yield f"event: snapshot\ndata: {snapshot.model_dump_json()}\n\n"
            heartbeat_at = time.monotonic()
            while True:
                if (
                    snapshot.status
                    in {
                        ExecutionStatus.SUCCEEDED,
                        ExecutionStatus.FAILED,
                        ExecutionStatus.CANCELLED,
                    }
                    and cursor >= high
                ):
                    return
                await asyncio.sleep(0.5)
                snapshot, high = await run_in_threadpool(read_head, job_id)
                while cursor < high:
                    events = await run_in_threadpool(repository.read_events, job_id, cursor, high)
                    for event in events:
                        cursor = event.sequence
                        yield _encode(event)
                        heartbeat_at = time.monotonic()
                if time.monotonic() - heartbeat_at >= 15:
                    yield ": heartbeat\n\n"
                    heartbeat_at = time.monotonic()
        except SQLAlchemyError:
            yield f"event: stream_error\ndata: {_stream_error(stage).model_dump_json()}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


def _encode(event: ExecutionEvent) -> str:
    payload = json.dumps(event.payload, ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.sequence}\nevent: {event.event_type}\ndata: {payload}\n\n"
