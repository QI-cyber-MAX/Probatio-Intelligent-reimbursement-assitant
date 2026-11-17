import asyncio
import logging
from collections.abc import Callable
from datetime import timedelta

from psycopg import Error as PsycopgError
from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.errors.execution import (
    exception_diagnostic,
    normalize_error,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionContext,
    ExecutionStatus,
    QueueDelivery,
    WorkflowResult,
)
from intelligent_reimbursement_assitant.execution_state_service.ports.workflow import (
    WorkflowExecutor,
)
from intelligent_reimbursement_assitant.worker_runtime.domain.config import (
    QueueBinding,
    WorkerSettings,
)
from intelligent_reimbursement_assitant.worker_runtime.ports.consuming import QueueConsumer
from intelligent_reimbursement_assitant.worker_runtime.ports.execution import (
    ExecutionControl,
    ExecutionRepository,
)

logger = logging.getLogger(__name__)


class WorkerRuntime:
    def __init__(
        self,
        consumer: QueueConsumer,
        repository: ExecutionRepository,
        state: ExecutionControl,
        executor: WorkflowExecutor,
        worker_id: str,
        settings: WorkerSettings,
        stop: asyncio.Event,
        *,
        binding: QueueBinding,
    ):
        self.consumer = consumer
        self.repository = repository
        self.state = state
        self.executor = executor
        self.worker_id = worker_id
        self.settings = settings
        self.binding = binding
        self.stop = stop
        self.inflight: set[asyncio.Task[None]] = set()
        self.executions: set[asyncio.Task[WorkflowResult]] = set()
        self.changed = asyncio.Event()
        self.forced_stop = False
        self.lease_duration = timedelta(seconds=settings.lease_seconds)

    def _transaction[T](self, operation: Callable[[Session], T]) -> T:
        with self.repository.session() as session, session.begin():
            result = operation(session)
        # Do not expose CLAIMED or successful disposition before commit confirmation.
        return result

    async def _wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop.wait(), seconds)
        except TimeoutError:
            pass

    async def run(self) -> None:
        consume = None
        try:
            await self.consumer.start()
            consume = asyncio.create_task(self._consume())
            await asyncio.shield(consume)
        finally:
            self.stop.set()
            # An acquired delivery or a running synchronous transaction is never abandoned.
            try:
                if consume is not None:
                    await consume
            finally:
                try:
                    await self.shutdown(self.settings.grace_seconds)
                finally:
                    await self.consumer.close()

    async def _consume(self) -> None:
        loop = asyncio.get_running_loop()
        next_reclaim = loop.time() + self.settings.reclaim_seconds
        while not self.stop.is_set():
            self.changed.clear()
            free = self.settings.concurrency - len(self.inflight)
            if not free:
                # Bounded wakeup also observes signals without cancelling delivery handlers.
                try:
                    await asyncio.wait_for(self.changed.wait(), 0.1)
                except TimeoutError:
                    pass
                continue
            try:
                if loop.time() >= next_reclaim:
                    next_reclaim = loop.time() + self.settings.reclaim_seconds
                    deliveries = await self.consumer.reclaim_pending(free)
                else:
                    deliveries = await self.consumer.receive(free)
                for delivery in deliveries:
                    task = asyncio.create_task(self._handle(delivery))
                    self.inflight.add(task)
                    task.add_done_callback(self._finished)
            except (RedisError, OSError) as error:
                logger.error(
                    "Queue read failed; Pending remains recoverable %s", exception_diagnostic(error)
                )
                await self._wait(1)

    def _finished(self, task: asyncio.Task[None]) -> None:
        self.inflight.discard(task)
        self.changed.set()
        if not task.cancelled() and (error := task.exception()) is not None:
            logger.error("Delivery handler failed %s", exception_diagnostic(error))

    async def _handle(self, delivery: QueueDelivery) -> None:
        try:
            if self.forced_stop:
                return
            try:
                claim = await asyncio.to_thread(
                    self._transaction,
                    lambda session: self.state.claim(
                        session,
                        delivery.job_id,
                        self.worker_id,
                        self.binding.kind,
                        self.lease_duration,
                    ),
                )
            except LookupError:
                await self.consumer.quarantine(delivery.entry_id, "Job does not exist")
                return
            if claim.outcome == "DEFER":
                logger.warning("Claim deferred job_id=%s reason=%s", delivery.job_id, claim.reason)
                await self._wait(1)
                return
            if claim.outcome == "SKIP":
                # Another executor still owns this lease; retain its delivery for recovery.
                if claim.reason != "ACTIVE_LEASE":
                    await self.consumer.ack(delivery.entry_id)
                return
            if self.forced_stop:
                return
            if await self._execute(claim.context):
                await self.consumer.ack(delivery.entry_id)
        except (SQLAlchemyError, PsycopgError, RedisError, OSError, ExecutionError) as error:
            logger.error(
                "Delivery not confirmed entry_id=%s job_id=%s %s",
                delivery.entry_id,
                delivery.job_id,
                exception_diagnostic(error),
            )

    async def _execute(self, ctx: ExecutionContext) -> bool:
        finished = asyncio.Event()
        uncertain = asyncio.Event()
        execution = asyncio.create_task(self.executor.execute(ctx))
        self.executions.add(execution)
        renewal = asyncio.create_task(self._renew(ctx, execution, finished, uncertain))
        try:
            try:
                result = await execution
            except asyncio.CancelledError:
                # Cancellation of the await does not certify stopped threads or external effects.
                return False
            except SQLAlchemyError, PsycopgError:
                # A lost publication commit receipt is infrastructure uncertainty.
                raise
            except Exception as failure:
                error = normalize_error(failure)
                logger.warning(
                    "Workflow failed job_id=%s code=%s category=%s %s",
                    ctx.job_id,
                    error.code,
                    error.category,
                    exception_diagnostic(failure),
                )
                if uncertain.is_set():
                    return False
                if error.code == ErrorCode.EXECUTION_CANCEL_REQUESTED:
                    # Only an executor's explicit stopping proof can reach this branch.
                    await asyncio.to_thread(
                        self._transaction, lambda session: self.state.confirm_cancel(session, ctx)
                    )
                else:
                    await asyncio.to_thread(
                        self._transaction,
                        lambda session: self.state.record_failure(session, ctx, error),
                    )
                return True
            if result.outcome == "SUCCEEDED":
                # Executor already committed publication + complete. Never write success twice.
                return True
            return True
        finally:
            finished.set()
            await renewal
            self.executions.discard(execution)

    async def _renew(
        self,
        ctx: ExecutionContext,
        execution: asyncio.Task[WorkflowResult],
        finished: asyncio.Event,
        uncertain: asyncio.Event,
    ) -> None:
        cancellation_sent = False
        while not finished.is_set():
            try:
                await asyncio.wait_for(finished.wait(), self.settings.renew_seconds)
                return
            except TimeoutError:
                pass
            try:
                status = await asyncio.to_thread(
                    self._transaction,
                    lambda session: self.state.renew_lease(session, ctx, self.lease_duration),
                )
            except (SQLAlchemyError, PsycopgError, ExecutionError) as error:
                if finished.is_set():
                    return
                uncertain.set()
                logger.error(
                    "Lease renewal uncertain or lost job_id=%s %s",
                    ctx.job_id,
                    exception_diagnostic(error),
                )
                if not execution.done() and not execution.cancelling():
                    execution.cancel()
                return
            if (
                status == ExecutionStatus.CANCELLING
                and not execution.done()
                and not cancellation_sent
            ):
                # One cancellation request; keep renewing while its cleanup runs.
                execution.cancel()
                cancellation_sent = True

    async def shutdown(self, grace_period: float) -> None:
        self.stop.set()
        if not self.inflight:
            return
        _, pending = await asyncio.wait(self.inflight, timeout=grace_period)
        if pending:
            self.forced_stop = True
            for execution in tuple(self.executions):
                if not execution.done() and not execution.cancelling():
                    execution.cancel()
            # Grace limits draining, not the duration of executor-owned resource cleanup.
            await asyncio.gather(*pending, return_exceptions=True)
