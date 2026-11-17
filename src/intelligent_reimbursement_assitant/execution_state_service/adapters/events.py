"""Append under the caller's execution-row lock; read with short independent queries."""

from collections.abc import Sequence
from typing import Literal
from uuid import UUID

from sqlalchemy import Engine, func, insert, select
from sqlalchemy.orm import Session, sessionmaker

from intelligent_reimbursement_assitant.errors.execution import public_terminal_reason
from intelligent_reimbursement_assitant.execution_state_service.adapters.schema import (
    execution_event,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.events import (
    EventPayload,
    EventType,
    ExecutionEvent,
    JobStatusEvent,
    PublishedMessageEvent,
    PublishedTextDelta,
)


def event_high_watermark(job_id: UUID):
    return (
        select(func.coalesce(func.max(execution_event.c.sequence), 0))
        .where(execution_event.c.job_id == job_id)
        .scalar_subquery()
    )


def append_events(
    session: Session, job_id: UUID, events: Sequence[tuple[EventType, EventPayload]]
) -> int | None:
    """Caller already holds execution_run FOR UPDATE, or inserted this new execution row."""
    if not events:
        return
    sequence = session.execute(select(event_high_watermark(job_id))).scalar_one()
    session.execute(
        insert(execution_event),
        [
            dict(
                job_id=job_id,
                sequence=sequence + index,
                event_type=kind,
                payload=payload.model_dump(mode="json"),
            )
            for index, (kind, payload) in enumerate(events, 1)
        ],
    )
    return sequence + len(events)


def append_status(session: Session, job_id: UUID, payload: JobStatusEvent) -> None:
    payload = payload.model_copy(
        update={
            "terminal_reason": public_terminal_reason(
                payload.last_error_code, payload.terminal_reason
            )
        }
    )
    append_events(session, job_id, [("status", payload)])


def append_published_text(
    session: Session,
    job_id: UUID,
    message_id: UUID,
    kind: Literal["clarification", "answer"],
    texts: Sequence[tuple[str | None, str]],
) -> None:
    events: list[tuple[EventType, EventPayload]] = []
    for item_id, text in texts:
        events.extend(
            (
                "text_delta",
                PublishedTextDelta(
                    message_id=message_id,
                    item_id=item_id,
                    offset=offset,
                    text=text[offset : offset + 512],
                ),
            )
            for offset in range(0, len(text), 512)
        )
    events.append((kind, PublishedMessageEvent(message_id=message_id)))
    append_events(session, job_id, events)


class SQLAlchemyEventRepository:
    def __init__(self, engine: Engine):
        self.sessions = sessionmaker(engine.execution_options(isolation_level="READ COMMITTED"))

    def session(self) -> Session:
        return self.sessions()

    def read_events(
        self, job_id: UUID, after: int, through: int | None = None
    ) -> list[ExecutionEvent]:
        query = select(execution_event).where(
            execution_event.c.job_id == job_id, execution_event.c.sequence > after
        )
        if through is not None:
            query = query.where(execution_event.c.sequence <= through)
        with self.session() as session:
            return [
                ExecutionEvent.model_validate(row)
                for row in session.execute(
                    query.order_by(execution_event.c.sequence).limit(200)
                ).mappings()
            ]
