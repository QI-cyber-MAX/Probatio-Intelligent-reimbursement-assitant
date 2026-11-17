import asyncio
import logging
import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import SQLAlchemyError

from intelligent_reimbursement_assitant.errors.execution import exception_diagnostic
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionKind,
    JobEnvelope,
)
from intelligent_reimbursement_assitant.outbox_publisher.domain.config import PublisherSettings
from intelligent_reimbursement_assitant.outbox_publisher.ports.publishing import (
    OutboxRepository,
    PublishError,
    StreamPublisher,
)

logger = logging.getLogger(__name__)
DEFAULT_SETTINGS = PublisherSettings()


@dataclass
class _StreamState:
    next_attempt: float = 0
    sampled_at: float = float("-inf")
    allowance: int = 0
    paused: bool = False
    failures: int = 0
    retry_at: float = 0


class OutboxPublisher:
    def __init__(
        self,
        repository: OutboxRepository,
        transport: StreamPublisher,
        groups: dict[ExecutionKind, tuple[str, str]],
        stop: asyncio.Event,
        settings: PublisherSettings = DEFAULT_SETTINGS,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        utcnow: Callable[[], datetime] = lambda: datetime.now(UTC),
        uniform: Callable[[float, float], float] = random.uniform,
    ):
        self.repository = repository
        self.transport = transport
        self.groups = groups
        self.stop = stop
        self.settings = settings
        self.monotonic = monotonic
        self.utcnow = utcnow
        self.uniform = uniform
        self._streams = {kind: _StreamState() for kind in groups}

    async def publish_pending(self, kind: ExecutionKind | None = None) -> None:
        # Each stream gets its own bounded read and pacing; a paused stream cannot starve another.
        kinds = self.groups if kind is None else (kind,)
        results = await asyncio.gather(
            *(self._publish_stream(item) for item in kinds), return_exceptions=True
        )
        for result in results:
            if isinstance(result, BaseException):
                raise result

    async def _sample(self, kind: ExecutionKind, state: _StreamState) -> bool:
        now = self.monotonic()
        if now - state.sampled_at >= self.settings.sample_interval or (
            not state.paused and state.allowance <= 0
        ):
            stream, group = self.groups[kind]
            pressure = await self.transport.read_group_pressure(stream, group)
            state.sampled_at = self.monotonic()
            was_paused = state.paused
            if pressure is None:
                state.paused = True
            elif state.paused:
                state.paused = pressure > self.settings.low_watermark
            else:
                state.paused = pressure >= self.settings.high_watermark
            state.allowance = (
                0 if pressure is None or state.paused else self.settings.high_watermark - pressure
            )
            if state.paused != was_paused:
                logger.warning(
                    "Stream pressure changed stream=%s group=%s paused=%s pressure=%s",
                    stream,
                    group,
                    state.paused,
                    pressure,
                )
        return not state.paused and state.allowance > 0

    async def _publish_stream(self, kind: ExecutionKind) -> None:
        state = self._streams[kind]
        if self.stop.is_set() or self.monotonic() < state.retry_at:
            return
        stream, _ = self.groups[kind]
        try:
            if not await self._sample(kind, state) or self.stop.is_set():
                return
            events = await asyncio.to_thread(
                self.repository.list_pending_events, kind, self.settings.batch_size
            )
            for event in events:
                if self.stop.is_set():
                    return
                delay = state.next_attempt - self.monotonic()
                if delay > 0:
                    try:
                        await asyncio.wait_for(self.stop.wait(), delay)
                        return
                    except TimeoutError:
                        pass
                if self.stop.is_set() or not await self._sample(kind, state):
                    return
                if self.stop.is_set():
                    return
                state.next_attempt = self.monotonic() + 1 / self.settings.attempts_per_second
                state.allowance -= 1
                entry_id = await self.transport.publish(
                    stream, JobEnvelope(job_id=event.job_id, kind=event.kind)
                )
                # A stop during XADD still requires its successful acknowledgement to be recorded.
                await asyncio.to_thread(
                    self.repository.mark_published, event.event_id, self.utcnow()
                )
                state.failures = 0
                state.retry_at = 0
                logger.info(
                    "Published event_id=%s stream=%s entry_id=%s", event.event_id, stream, entry_id
                )
        except (PublishError, SQLAlchemyError) as error:
            state.failures += 1
            cap = min(
                self.settings.backoff_max,
                self.settings.backoff_initial * 2 ** min(state.failures - 1, 30),
            )
            state.retry_at = self.monotonic() + self.uniform(0, cap)
            # Both XADD response loss and PG confirmation failure consume the sampled allowance.
            logger.error(
                "Outbox publication failed stream=%s retry_at=%s %s",
                stream,
                state.retry_at,
                exception_diagnostic(error),
            )
