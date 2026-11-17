from datetime import datetime
from typing import Protocol
from uuid import UUID

from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionKind,
    JobEnvelope,
    OutboxEvent,
)


class PublishError(Exception):
    """Redis operation failed or its outcome is unknown; never a business failure."""


class OutboxRepository(Protocol):
    def list_pending_events(self, kind: ExecutionKind, limit: int) -> list[OutboxEvent]: ...
    def mark_published(self, event_id: UUID, now: datetime) -> None: ...


class StreamPublisher(Protocol):
    async def publish(self, stream: str, envelope: JobEnvelope) -> str: ...
    async def read_group_pressure(self, stream: str, group: str) -> int | None: ...
