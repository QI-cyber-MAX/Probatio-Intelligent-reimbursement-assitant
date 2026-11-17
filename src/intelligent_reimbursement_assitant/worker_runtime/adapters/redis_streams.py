import asyncio
import base64
import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from pydantic import ValidationError
from redis.asyncio import Redis
from redis.asyncio.connection import ConnectionPool
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError, ResponseError

from intelligent_reimbursement_assitant.errors.execution import exception_diagnostic
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    JobEnvelope,
    QueueDelivery,
)
from intelligent_reimbursement_assitant.worker_runtime.domain.config import (
    QueueBinding,
    WorkerSettings,
)

logger = logging.getLogger(__name__)
type RawEntries = list[tuple[bytes, dict[bytes, bytes]]]


class RedisQueueConsumer:
    def __init__(
        self,
        client: Redis,
        worker_id: str,
        settings: WorkerSettings,
        record_path: Path,
        *,
        binding: QueueBinding,
    ):
        """Own a binary RESP2 client so arbitrary malformed bytes remain recoverable."""
        self.client = client
        self.worker_id = worker_id
        self.settings = settings
        self.binding = binding
        self.record_path = record_path
        self.cursor = b"0-0"

    @classmethod
    def from_url(
        cls,
        url: str,
        worker_id: str,
        settings: WorkerSettings,
        record_path: Path,
        *,
        binding: QueueBinding,
    ) -> RedisQueueConsumer:
        pool = ConnectionPool.from_url(url)
        pool.connection_kwargs.update(
            decode_responses=False,
            protocol=2,
            socket_connect_timeout=settings.redis_connect_timeout,
            socket_timeout=settings.redis_command_timeout,
            retry=Retry(NoBackoff(), 0),
            retry_on_error=[],
        )
        return cls(Redis(connection_pool=pool), worker_id, settings, record_path, binding=binding)

    async def start(self) -> None:
        try:
            await self.client.xgroup_create(
                self.binding.stream, self.binding.group, id="0-0", mkstream=True
            )
        except ResponseError as error:
            if str(error) != "BUSYGROUP Consumer Group name already exists":
                raise

    async def receive(self, limit: int) -> list[QueueDelivery]:
        response = cast(
            list[tuple[bytes, RawEntries]],
            await self.client.xreadgroup(
                self.binding.group,
                self.worker_id,
                {self.binding.stream: ">"},
                count=limit,
                block=self.settings.read_block_ms,
            ),
        )
        entries = response[0][1] if response else []
        return await self._parse(entries)

    async def reclaim_pending(self, limit: int) -> list[QueueDelivery]:
        cursor, entries, deleted = cast(
            tuple[bytes, RawEntries, list[bytes]],
            await self.client.xautoclaim(
                self.binding.stream,
                self.binding.group,
                self.worker_id,
                self.settings.pending_idle_ms,
                start_id=self.cursor,
                count=limit,
            ),
        )
        self.cursor = cursor
        if deleted:
            logger.warning(
                "Pending entries were deleted externally stream=%s ids=%s",
                self.binding.stream,
                deleted,
            )
        return await self._parse(entries)

    async def _parse(self, entries: RawEntries) -> list[QueueDelivery]:
        deliveries = []
        for raw_id, raw_fields in entries:
            entry_id = raw_id.decode("ascii")
            try:
                envelope = JobEnvelope.model_validate(
                    {
                        key.decode("utf-8"): value.decode("utf-8")
                        for key, value in raw_fields.items()
                    }
                )
                if envelope.kind != self.binding.kind:
                    raise ValueError("Envelope kind does not match this worker pool")
            except UnicodeError, ValueError, ValidationError:
                # Each acquired entry remains pending if recording or ACK fails.
                try:
                    await self._record(
                        entry_id, raw_fields, "Invalid queue message or worker kind mismatch"
                    )
                    await self.ack(entry_id)
                except (OSError, RedisError) as failure:
                    # Redis errors as well as disk errors must not discard other acquired entries.
                    logger.error(
                        "Message isolation failed entry_id=%s %s",
                        entry_id,
                        exception_diagnostic(failure),
                    )
                continue
            deliveries.append(
                QueueDelivery(job_id=envelope.job_id, kind=envelope.kind, entry_id=entry_id)
            )
        return deliveries

    async def quarantine(self, entry_id: str, reason: str) -> None:
        entries = cast(
            RawEntries, await self.client.xrange(self.binding.stream, entry_id, entry_id, count=1)
        )
        if not entries:
            raise OSError("Cannot record missing Stream entry")
        await self._record(entry_id, entries[0][1], reason)
        await self.ack(entry_id)

    async def _record(self, entry_id: str, raw_fields: dict[bytes, bytes], reason: str) -> None:
        record = dict(
            recorded_at=datetime.now(UTC).isoformat(),
            stream=self.binding.stream,
            group=self.binding.group,
            entry_id=entry_id,
            reason=reason,
            raw_fields={
                base64.b64encode(k).decode("ascii"): base64.b64encode(v).decode("ascii")
                for k, v in raw_fields.items()
            },
        )
        await asyncio.to_thread(self._append, json.dumps(record, ensure_ascii=True) + "\n")

    def _append(self, line: str) -> None:
        self.record_path.parent.mkdir(parents=True, exist_ok=True)
        # Each process has its own file. A failed partial append was never ACKed;
        # discard only that incomplete tail before retrying, preserving readable JSONL.
        with self.record_path.open("a+b") as output:
            output.seek(0, os.SEEK_END)
            position = output.tell()
            if position:
                output.seek(position - 1)
                if output.read(1) != b"\n":
                    while position:
                        start = max(0, position - 4096)
                        output.seek(start)
                        last_newline = output.read(position - start).rfind(b"\n")
                        if last_newline >= 0:
                            position = start + last_newline + 1
                            break
                        position = start
                    output.truncate(position)
            output.write(line.encode("utf-8"))
            output.flush()
            os.fsync(output.fileno())
        # Also persist creation of queue_anomalies/ under the existing managed root.
        for parent in (self.record_path.parent, self.record_path.parent.parent):
            directory = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    async def ack(self, entry_id: str) -> None:
        await self.client.xack(self.binding.stream, self.binding.group, entry_id)

    async def close(self) -> None:
        await self.client.aclose(close_connection_pool=True)
