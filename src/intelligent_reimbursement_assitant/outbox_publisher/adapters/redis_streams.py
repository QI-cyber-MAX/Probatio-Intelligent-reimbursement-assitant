import logging
from typing import cast

from redis.asyncio import Redis
from redis.asyncio.connection import ConnectionPool
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError, ResponseError

from intelligent_reimbursement_assitant.execution_state_service.domain.models import JobEnvelope
from intelligent_reimbursement_assitant.outbox_publisher.ports.publishing import PublishError

logger = logging.getLogger(__name__)


class RedisStreamPublisher:
    def __init__(self, client: Redis):
        """Client uses decode_responses=True, RESP2 and no SDK retries."""
        self.client = client

    @classmethod
    def from_url(
        cls, url: str, *, connect_timeout: float = 2, command_timeout: float = 3
    ) -> RedisStreamPublisher:
        # Build the pool explicitly: URL query parameters must not override retry/timeout policy.
        pool = ConnectionPool.from_url(url)
        pool.connection_kwargs.update(
            decode_responses=True,
            protocol=2,
            socket_connect_timeout=connect_timeout,
            socket_timeout=command_timeout,
            retry=Retry(NoBackoff(), 0),
            retry_on_error=[],
        )
        client = Redis(connection_pool=pool)
        return cls(client)

    async def publish(self, stream: str, envelope: JobEnvelope) -> str:
        try:
            return cast(
                str,
                await self.client.xadd(
                    stream, {"job_id": str(envelope.job_id), "kind": envelope.kind.value}
                ),
            )
        except RedisError as error:
            raise PublishError(f"XADD failed for stream={stream}") from error

    async def read_group_pressure(self, stream: str, group: str) -> int | None:
        try:
            groups = await self.client.xinfo_groups(stream)
        except ResponseError as error:
            if str(error) != "no such key":
                raise PublishError(f"XINFO GROUPS failed for stream={stream}") from error
            logger.warning("Stream not established stream=%s group=%s", stream, group)
            return None
        except RedisError as error:
            raise PublishError(f"XINFO GROUPS failed for stream={stream}") from error
        for item in groups:
            if item["name"] == group:
                if item["lag"] is None:
                    logger.warning("Group lag unknown stream=%s group=%s", stream, group)
                    return None
                return item["lag"] + item["pending"]
        logger.warning("Consumer group not established stream=%s group=%s", stream, group)
        return None

    async def close(self) -> None:
        await self.client.aclose(close_connection_pool=True)
