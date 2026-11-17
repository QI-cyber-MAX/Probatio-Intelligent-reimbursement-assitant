"""Native graph steps save node output and Send parameters before continuing."""

import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import TypedDict, cast
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import Command, Send, interrupt

from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionContext,
)
from intelligent_reimbursement_assitant.policy_qa_graph.application.evidence import (
    route_assessments,
)
from intelligent_reimbursement_assitant.policy_qa_graph.application.retrieval import (
    prepare_retrieval_round,
    retrieval_allowed,
    retrieval_round_allowed,
)
from intelligent_reimbursement_assitant.policy_qa_graph.domain.state import (
    QAState,
    restore_tool_admissions,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.config import QASettings
from intelligent_reimbursement_assitant.policy_qa_service.domain.models import (
    AssessDecision,
    ClarificationRequest,
    TaskPlan,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.publication import (
    assemble_answer_result,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.tool_calling import (
    select_retrieval_items,
)
from intelligent_reimbursement_assitant.policy_qa_service.ports.repository import QARepository

type Node = Callable[..., Awaitable[QAState]]


@dataclass(frozen=True)
class QANodes:
    understand: Node
    apply_clarification: Node
    coordinate_tools: Callable[..., AsyncGenerator[QAState]]
    assess_evidence: Node
    prepare_evidence_clarification: Node
    generate_answer: Node


@dataclass
class QARuntime:
    ctx: ExecutionContext
    nodes: QANodes
    settings: QASettings
    repository: QARepository
    retrieval: AsyncGenerator[QAState] | None = None

    async def close_retrieval(self) -> None:
        generator, self.retrieval = self.retrieval, None
        if generator is None:
            return

        async def finish():
            async with aclosing(generator):
                pass

        closing = asyncio.create_task(finish())
        cancelled = False
        while True:
            try:
                await asyncio.shield(closing)
                break
            except asyncio.CancelledError:
                if closing.cancelled():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError


class ReplyTask(TypedDict):
    state: QAState
    reply_message_id: str


async def retrieval_step(state: QAState, runtime: Runtime[QARuntime]) -> Command:
    env = runtime.context
    if env.retrieval is None:
        plan = cast(TaskPlan, state["plan"])
        env.retrieval = env.nodes.coordinate_tools(
            restore_tool_admissions(dict(state)),
            select_retrieval_items(plan, state["assessments"]),
            env.ctx,
        )
    update = await anext(env.retrieval)
    done = isinstance(update["decision"], AssessDecision)
    if done:
        await env.close_retrieval()
    return Command(update=update, goto="assess" if done else "retrieve")


def build_qa_graph(checkpointer: BaseCheckpointSaver):
    async def understand(state: QAState, runtime: Runtime[QARuntime]):
        env = runtime.context
        return await env.nodes.understand(state, env.ctx)

    async def after_understanding(state: QAState, runtime: Runtime[QARuntime]):
        env = runtime.context
        if state["clarification"] is not None:
            return Command(goto="prepare_wait")
        allowed = retrieval_round_allowed(state, env.settings) and await retrieval_allowed(
            env.repository,
            env.ctx,
            env.settings,
        )
        return Command(
            update=prepare_retrieval_round(state) if allowed else {},
            goto="retrieve" if allowed else "assess",
        )

    async def assess(state: QAState, runtime: Runtime[QARuntime]):
        env = runtime.context
        return await env.nodes.assess_evidence(state, env.ctx)

    async def route_evidence(state: QAState, runtime: Runtime[QARuntime]):
        env = runtime.context
        route = route_assessments(state, env.settings)
        if route == "retrieve" and not await retrieval_allowed(
            env.repository, env.ctx, env.settings
        ):
            route = "answer" if any(a.claims for a in state["assessments"]) else "restricted"
        return Command(
            update=prepare_retrieval_round(state) if route == "retrieve" else {},
            goto={
                "answer": "answer",
                "retrieve": "retrieve",
                "clarification": "clarify",
                "restricted": "finalize",
            }[route],
        )

    async def clarify(state: QAState, runtime: Runtime[QARuntime]):
        env = runtime.context
        return await env.nodes.prepare_evidence_clarification(state, env.ctx)

    async def after_clarify(state: QAState, runtime: Runtime[QARuntime]):
        if state["clarification"] is not None:
            return Command(goto="prepare_wait")
        env = runtime.context
        route = route_assessments(state, env.settings, allow_clarification=False)
        if route == "retrieve" and await retrieval_allowed(env.repository, env.ctx, env.settings):
            return Command(update=prepare_retrieval_round(state), goto="retrieve")
        if any(a.claims for a in state["assessments"]):
            return Command(goto="answer")
        return Command(goto="finalize")

    def prepare_wait(state: QAState):
        return {"clarification_count": state["clarification_count"] + 1}

    def wait(state: QAState):
        # Pure native interrupt: no IO, accounting or prompt generation on replay.
        reply = interrupt(
            cast(ClarificationRequest, state["clarification"]).model_dump(mode="json")
        )
        return Command(
            goto=Send("apply_reply", {"state": state, "reply_message_id": reply["message_id"]})
        )

    async def apply_reply(task: ReplyTask, runtime: Runtime[QARuntime]):
        env = runtime.context
        update = await env.nodes.apply_clarification(
            task["state"],
            env.ctx,
            reply_message_id=UUID(task["reply_message_id"]),
        )
        return Command(
            update=update, goto="prepare_wait" if update["clarification"] is not None else "assess"
        )

    async def answer(state: QAState, runtime: Runtime[QARuntime]):
        env = runtime.context
        return await env.nodes.generate_answer(state, env.ctx)

    def finalize(state: QAState):
        return {
            "result": assemble_answer_result(
                cast(TaskPlan, state["plan"]),
                state["answer_sections"],
                state["assessments"],
                state["evidence_chunks"],
                state["input"].snapshot,
            )
        }

    graph = StateGraph(QAState, context_schema=QARuntime)
    for name, node in {
        "understand": understand,
        "after_understanding": after_understanding,
        "retrieve": retrieval_step,
        "assess": assess,
        "route_evidence": route_evidence,
        "clarify": clarify,
        "after_clarify": after_clarify,
        "prepare_wait": prepare_wait,
        "wait": wait,
        "apply_reply": apply_reply,
        "answer": answer,
        "finalize": finalize,
    }.items():
        graph.add_node(name, node)
    for source, target in [
        (START, "understand"),
        ("understand", "after_understanding"),
        ("assess", "route_evidence"),
        ("clarify", "after_clarify"),
        ("prepare_wait", "wait"),
        ("answer", "finalize"),
        ("finalize", END),
    ]:
        graph.add_edge(source, target)
    return graph.compile(checkpointer=checkpointer)
