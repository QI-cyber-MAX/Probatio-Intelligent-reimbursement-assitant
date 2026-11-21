"""Compose the queue consumer, worker runtime and injected workflow executor."""

import asyncio
import signal
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from pathlib import Path
from uuid import uuid4

from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import ArgumentError

from intelligent_reimbursement_assitant.execution_state_service.adapters.repository import (
    SQLAlchemyExecutionRepository,
)
from intelligent_reimbursement_assitant.execution_state_service.application.service import (
    ExecutionStateService,
)
from intelligent_reimbursement_assitant.execution_state_service.ports.workflow import (
    WorkflowExecutor,
)
from intelligent_reimbursement_assitant.worker_runtime.adapters.redis_streams import (
    RedisQueueConsumer,
)
from intelligent_reimbursement_assitant.worker_runtime.application.runtime import WorkerRuntime
from intelligent_reimbursement_assitant.worker_runtime.domain.config import (
    QueueBinding,
    WorkerSettings,
)


async def serve_worker(
    database_url: str,
    redis_url: str,
    managed_directory: Path,
    executor_factory: Callable[
        [Engine, ExecutionStateService], AbstractAsyncContextManager[WorkflowExecutor]
    ],
    settings: WorkerSettings | None = None,
    *,
    binding: QueueBinding,
) -> None:
    settings = settings or WorkerSettings()
    worker_id = str(uuid4())
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    try:
        engine = create_engine(
            database_url, connect_args={"connect_timeout": 2}, pool_pre_ping=True
        )
    except ArgumentError, ValueError:
        raise ValueError("worker.database_url: connection_configuration") from None
    resources = AsyncExitStack()
    consumer = None
    runtime_owns_consumer = False
    registered_signals = []
    try:
        state = ExecutionStateService()
        executor = await resources.enter_async_context(executor_factory(engine, state))
        try:
            consumer = RedisQueueConsumer.from_url(
                redis_url,
                worker_id,
                settings,
                managed_directory / "queue_anomalies" / f"{worker_id}.jsonl",
                binding=binding,
            )
        except ValueError:
            raise ValueError("worker.redis_url: connection_configuration") from None
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
            registered_signals.append(signum)
        runtime = WorkerRuntime(
            consumer,
            SQLAlchemyExecutionRepository(engine),
            state,
            executor,
            worker_id,
            settings,
            stop,
            binding=binding,
        )
        runtime_owns_consumer = True
        await runtime.run()
    finally:
        # Runtime owns normal/start-failure closure; entry owns partial initialization.
        try:
            if consumer is not None and not runtime_owns_consumer:
                await consumer.close()
        finally:
            try:
                await resources.aclose()
            finally:
                try:
                    await asyncio.to_thread(engine.dispose)
                finally:
                    for signum in registered_signals:
                        loop.remove_signal_handler(signum)
