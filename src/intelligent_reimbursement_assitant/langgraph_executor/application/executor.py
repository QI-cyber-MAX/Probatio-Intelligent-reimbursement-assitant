"""One Job owns one native Thread and one execution-local resource context."""

import asyncio
from collections.abc import Callable
from typing import cast

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.types import Command

from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.execution_state_service.domain.events import (
    ProgressFactory,
    ProgressStage,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionContext,
    WorkflowResult,
    WorkflowSucceeded,
    WorkflowWaiting,
)
from intelligent_reimbursement_assitant.langgraph_executor.ports.recovery import QARecoveryReader
from intelligent_reimbursement_assitant.model_gateway.domain.config import ModelSettings
from intelligent_reimbursement_assitant.policy_qa_graph.application.graph import (
    QANodes,
    QARuntime,
    build_qa_graph,
)
from intelligent_reimbursement_assitant.policy_qa_graph.domain.state import QAState
from intelligent_reimbursement_assitant.policy_qa_service.application.service import PolicyQAService
from intelligent_reimbursement_assitant.policy_qa_service.domain.models import (
    ClarificationRequest,
    QAInput,
)


def initial_state(service: PolicyQAService, ctx: ExecutionContext) -> QAState:
    with service.repository.session() as session:
        turn = service.repository.load_turn(session, ctx.job_id)
        root = service.repository.load_messages(session, [turn.message_id])[0]
    return QAState(
        message_id=root.message_id,
        input=cast(QAInput, root.payload),
        reference_time=root.created_at,
        reply_message_ids=[],
        plan=None,
        fact_values={},
        search_queries=[],
        tool_sessions={},
        chunk_history_by_item={},
        decision=None,
        pending_tool_executions=[],
        search_batches=[],
        ranked_chunk_ids={},
        evidence_chunks={},
        evidence_ids_by_item={},
        retrieval_notes={},
        assessments=[],
        clarification=None,
        answer_sections=[],
        retrieval_round=0,
        tool_steps=0,
        clarification_count=0,
        result=None,
    )


class LangGraphExecutor:
    def __init__(
        self,
        service: PolicyQAService,
        nodes: QANodes,
        checkpoints: QARecoveryReader,
        saver_factory: Callable[[ExecutionContext], BaseCheckpointSaver],
        *,
        progress: ProgressFactory | None = None,
    ):
        self.service = service
        self.nodes = nodes
        self.checkpoints = checkpoints
        self.saver_factory = saver_factory
        self.progress = progress

    async def execute(self, ctx: ExecutionContext) -> WorkflowResult:
        if await asyncio.to_thread(self.service.load_answer, ctx.job_id) is not None:
            return WorkflowSucceeded()
        recovery = await asyncio.to_thread(self.checkpoints.read, ctx.job_id)
        if not recovery.safe:
            raise ExecutionError(
                ErrorCode.QA_MODEL_OUTCOME_UNKNOWN,
                "OUTCOME_UNKNOWN",
                "Native execution does not confirm a safe continuation",
            )
        if recovery.state is not None and recovery.state["plan"] is not None:
            await asyncio.to_thread(self.service.ensure_history_scope, recovery.state["input"])
        if recovery.state is not None and recovery.state["result"] is not None:
            if self.progress is not None:
                await self.progress(ctx, None, None)(ProgressStage.PUBLISHING_ANSWER)
            await asyncio.to_thread(self.service.publish_answer, ctx, recovery.state["result"])
            return WorkflowSucceeded()
        graph_input = None
        if recovery.checkpoint is None:
            graph_input = await asyncio.to_thread(initial_state, self.service, ctx)
        elif recovery.snapshot.interrupts:
            pending = recovery.snapshot.interrupts[0]

            def accepted_reply():
                with self.service.repository.session() as session:
                    turn = self.service.repository.load_turn(session, ctx.job_id)
                    prompt = self.service.repository.find_prompt(
                        session, turn.message_id, pending.id
                    )
                    return (
                        self.service.repository.find_reply(session, prompt.message_id)
                        if prompt is not None
                        else None
                    )

            reply = await asyncio.to_thread(accepted_reply)
            if reply is None:
                request = cast(ClarificationRequest, cast(QAState, recovery.state)["clarification"])
                message = await asyncio.to_thread(
                    self.service.enter_waiting_input, ctx, pending.id, request
                )
                return WorkflowWaiting(waiting_ref=str(message.message_id))
            if pending.id not in recovery.resume_ids:
                graph_input = Command(resume={pending.id: {"message_id": str(reply.message_id)}})
        env = QARuntime(ctx, self.nodes, self.service.settings, self.service.repository)
        saver: BaseCheckpointSaver = self.saver_factory(ctx)
        graph = build_qa_graph(saver)
        # A response can yield admission/completion states for every tool. The output
        # capacity bounds the number of TaskItems; do not let the default 25 steps
        # truncate a valid multi-item round before the shared business limits.
        capacity = cast(ModelSettings, self.service.settings.model).max_output_tokens
        limit = 128 + 8 * (self.service.settings.tool_step_limit + capacity) * (
            self.service.settings.retrieval_round_limit
            + self.service.settings.clarification_limit
            + 1
        )
        try:
            await graph.ainvoke(
                graph_input,
                {"configurable": {"thread_id": str(ctx.job_id)}, "recursion_limit": limit},
                context=env,
                durability="sync",
            )
        except BaseException as primary:
            try:
                await env.close_retrieval()
            except BaseException as closing:
                if isinstance(primary, ExecutionError) and primary.category == "OUTCOME_UNKNOWN":
                    raise primary from primary.__cause__
                if isinstance(closing, asyncio.CancelledError):
                    raise primary from primary.__cause__
                raise
            raise
        else:
            await env.close_retrieval()
        # Reading the committed native result also covers successful pending writes.
        saved = await asyncio.to_thread(self.checkpoints.read, ctx.job_id)
        if saved.state is not None and saved.state["result"] is not None:
            if self.progress is not None:
                await self.progress(ctx, None, None)(ProgressStage.PUBLISHING_ANSWER)
            await asyncio.to_thread(self.service.publish_answer, ctx, saved.state["result"])
            return WorkflowSucceeded()
        pending = saved.snapshot.interrupts[0]
        message = await asyncio.to_thread(
            self.service.enter_waiting_input,
            ctx,
            pending.id,
            cast(ClarificationRequest, cast(QAState, saved.state)["clarification"]),
        )
        return WorkflowWaiting(waiting_ref=str(message.message_id))
