"""Maintenance composition root. No migrations and no consumer-group creation."""

import asyncio
import logging
import signal
from collections.abc import Callable
from contextlib import AsyncExitStack
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import cast

from psycopg import Error as PsycopgError
from sqlalchemy import create_engine
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

from intelligent_reimbursement_assitant.dispatcher.application.service import Dispatcher
from intelligent_reimbursement_assitant.dispatcher.domain.config import MaintenanceSettings
from intelligent_reimbursement_assitant.document_ingestion_service.adapters.repository import (
    SQLAlchemyDocumentRepository,
)
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.errors.execution import exception_diagnostic
from intelligent_reimbursement_assitant.execution_state_service.adapters.repository import (
    SQLAlchemyExecutionRepository,
)
from intelligent_reimbursement_assitant.execution_state_service.application.service import (
    ExecutionStateService,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import ExecutionKind
from intelligent_reimbursement_assitant.ingestion_executor.application.completion import (
    reconcile_ingestion_completion,
)
from intelligent_reimbursement_assitant.knowledge_wiki.adapters.repository import (
    SQLAlchemyWikiRepository,
)
from intelligent_reimbursement_assitant.knowledge_wiki.application.completion import (
    reconcile_wiki_completion,
)
from intelligent_reimbursement_assitant.knowledge_wiki.application.service import (
    discover_wiki_updates,
)
from intelligent_reimbursement_assitant.knowledge_wiki.domain.config import WikiSettings
from intelligent_reimbursement_assitant.langgraph_executor.adapters.checkpoints import (
    PostgresQARecoveryReader,
)
from intelligent_reimbursement_assitant.langgraph_executor.application.completion import (
    reconcile_qa_completion,
)
from intelligent_reimbursement_assitant.outbox_publisher.adapters.redis_streams import (
    RedisStreamPublisher,
)
from intelligent_reimbursement_assitant.outbox_publisher.application.service import OutboxPublisher
from intelligent_reimbursement_assitant.policy_qa_service.adapters.repository import (
    SQLAlchemyQARepository,
)

logger = logging.getLogger(__name__)


async def _wait(stop: asyncio.Event, seconds: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), seconds)
    except TimeoutError:
        pass


async def run_maintenance(
    dispatcher: Dispatcher,
    publisher: OutboxPublisher,
    stop: asyncio.Event,
    settings: MaintenanceSettings,
    *,
    discover: Callable[[], None] | None = None,
    discovery_interval: float = 259200,
) -> None:
    async def dispatch_loop() -> None:
        while not stop.is_set():
            try:
                await asyncio.to_thread(dispatcher.tick, datetime.now(UTC))
            except (SQLAlchemyError, PsycopgError) as error:
                logger.error(
                    "Dispatcher scan failed; retrying next scan %s", exception_diagnostic(error)
                )
            await _wait(stop, settings.dispatcher_interval)

    async def publish_loop(kind: ExecutionKind) -> None:
        while not stop.is_set():
            await publisher.publish_pending(kind)
            await _wait(stop, settings.publisher_interval)

    async def discovery_loop() -> None:
        while not stop.is_set():
            try:
                await asyncio.to_thread(cast(Callable[[], None], discover))
            except (SQLAlchemyError, PsycopgError, ExecutionError) as error:
                logger.error(
                    "Wiki discovery failed; retrying next scan %s", exception_diagnostic(error)
                )
            await _wait(stop, discovery_interval)

    tasks = [asyncio.create_task(dispatch_loop())]
    if discover is not None:
        tasks.append(asyncio.create_task(discovery_loop()))
    tasks.extend(asyncio.create_task(publish_loop(kind)) for kind in publisher.groups)
    combined = asyncio.gather(*tasks)
    try:
        # Cancellation of the caller must not cancel an in-flight Redis/PG operation or thread.
        await asyncio.shield(combined)
    finally:
        stop.set()
        await asyncio.gather(*tasks, return_exceptions=True)


async def _serve(
    database_url: str,
    redis_url: str,
    settings: MaintenanceSettings,
    groups: dict[ExecutionKind, tuple[str, str]],
    wiki: WikiSettings | None = None,
) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    async with AsyncExitStack() as resources:
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
            resources.callback(loop.remove_signal_handler, signum)
        try:
            engine = create_engine(
                database_url, connect_args={"connect_timeout": 2}, pool_pre_ping=True
            )
        except ArgumentError, ValueError:
            raise ValueError("maintenance.database_url: connection_configuration") from None
        resources.push_async_callback(asyncio.to_thread, engine.dispose)
        try:
            transport = RedisStreamPublisher.from_url(
                redis_url,
                connect_timeout=settings.redis_connect_timeout,
                command_timeout=settings.redis_command_timeout,
            )
        except ValueError:
            raise ValueError("maintenance.redis_url: connection_configuration") from None
        resources.push_async_callback(transport.close)
        repository = SQLAlchemyExecutionRepository(engine)
        dispatcher = Dispatcher(
            repository,
            ExecutionStateService(),
            batch_size=settings.dispatcher_batch_size,
            stale_after=timedelta(seconds=settings.queued_stale_seconds),
            wakeup_interval=timedelta(seconds=settings.wakeup_interval_seconds),
            stopping=stop.is_set,
            completion_checks={
                ExecutionKind.WIKI_BUILD: partial(
                    reconcile_wiki_completion, SQLAlchemyWikiRepository(engine)
                ),
                ExecutionKind.INGESTION: partial(
                    reconcile_ingestion_completion, SQLAlchemyDocumentRepository(engine)
                ),
                ExecutionKind.AGENT_TURN: partial(
                    reconcile_qa_completion,
                    SQLAlchemyQARepository(engine),
                    PostgresQARecoveryReader(engine),
                ),
            },
        )
        publisher = OutboxPublisher(
            repository,
            transport,
            groups,
            stop,
            settings.publisher,
        )
        discover = (
            partial(
                discover_wiki_updates,
                SQLAlchemyWikiRepository(engine),
                SQLAlchemyDocumentRepository(engine),
                ExecutionStateService(),
                wiki.execution,
                batch_size=settings.dispatcher_batch_size,
                stopping=stop.is_set,
            )
            if wiki is not None
            else None
        )
        await run_maintenance(
            dispatcher,
            publisher,
            stop,
            settings,
            discover=discover,
            discovery_interval=wiki.discovery_interval_seconds if wiki is not None else 259200,
        )
