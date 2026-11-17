"""Bind a progress writer to an execution and, when present, its item and tool call."""

import asyncio

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.execution_state_service.adapters.events import (
    SQLAlchemyEventRepository,
    append_events,
)
from intelligent_reimbursement_assitant.execution_state_service.adapters.schema import (
    execution_event,
)
from intelligent_reimbursement_assitant.execution_state_service.application.service import (
    ExecutionStateService,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.events import (
    PROGRESS_TEXT,
    JobProgress,
    ProgressStage,
    ReportProgress,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionContext,
)


def bind_progress(
    repository: SQLAlchemyEventRepository,
    state: ExecutionStateService,
    ctx: ExecutionContext,
    item_id: str | None = None,
    tool_call_id: str | None = None,
) -> ReportProgress:
    def write(stage: ProgressStage) -> None:
        payload = JobProgress(
            stage=stage, item_id=item_id, tool_call_id=tool_call_id, text=PROGRESS_TEXT[stage]
        )
        sequence = None
        commit_started = False
        try:
            with repository.session() as session:
                if not state.check_execution(session, ctx):
                    session.commit()
                    raise ExecutionError(
                        ErrorCode.EXECUTION_LEASE_LOST, "ABORT", "Execution stopped"
                    )
                sequence = append_events(session, ctx.job_id, [("progress", payload)])
                commit_started = True
                session.commit()
        except SQLAlchemyError as error:
            if commit_started:
                try:
                    with repository.session() as session:
                        if not state.check_execution(session, ctx):
                            session.commit()
                            raise ExecutionError(
                                ErrorCode.EXECUTION_LEASE_LOST, "ABORT", "Execution stopped"
                            )
                        confirmed = session.scalar(
                            select(execution_event.c.sequence).where(
                                execution_event.c.job_id == ctx.job_id,
                                execution_event.c.sequence == sequence,
                                execution_event.c.event_type == "progress",
                                execution_event.c.payload == payload.model_dump(mode="json"),
                            )
                        )
                        session.commit()
                        if confirmed is not None:
                            return
                except SQLAlchemyError:
                    pass
            # The action following this start event has not been issued.
            raise ExecutionError(
                ErrorCode.EXECUTION_INTERRUPTED,
                "TASK_RETRYABLE",
                "Progress commit not confirmed; action was not issued",
            ) from error

    async def report(stage: ProgressStage) -> None:
        # Finish the short transaction before cancellation releases its resources.
        task = asyncio.create_task(asyncio.to_thread(write, stage))
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            task.result()
            raise

    return report
