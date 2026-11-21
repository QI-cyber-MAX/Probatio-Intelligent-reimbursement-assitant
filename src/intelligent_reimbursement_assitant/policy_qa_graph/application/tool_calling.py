"""Concurrent model proposals with one owner of admission and tool execution.

The caller must reliably save every yielded state before advancing the iterator.
Use contextlib.aclosing so a save failure settles outstanding requests before
the resource owner closes HTTP and database resources.
execute_tool returns only the operation product; this coordinator merges shared pools. No backend or checkpoint implementation is implied.
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from functools import partial
from typing import Any, Literal, Protocol, cast
from uuid import UUID

from pydantic import JsonValue

from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    SnapshotEntry,
)
from intelligent_reimbursement_assitant.errors.codes import ErrorCode as Code
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.errors.execution import gather_settled, run_io
from intelligent_reimbursement_assitant.execution_state_service.application.service import (
    ExecutionStateService,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.events import (
    ProgressFactory,
    ProgressStage,
    ReportProgress,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionContext,
)
from intelligent_reimbursement_assitant.knowledge_wiki.ports.reading import WikiReaderPort
from intelligent_reimbursement_assitant.model_gateway.domain.config import ModelSettings
from intelligent_reimbursement_assitant.model_gateway.ports.model_gateway import ModelGateway
from intelligent_reimbursement_assitant.policy_qa_graph.application.retrieval import (
    ToolInput,
    ToolOutput,
    _batch_key,
    retrieval_allowed,
)
from intelligent_reimbursement_assitant.policy_qa_graph.domain.state import QAState
from intelligent_reimbursement_assitant.policy_qa_service.application.model_call import (
    NativeCall,
    ToolResponse,
    call_tools,
    check_execution,
    consecutive_tool_response_errors,
    pending_tool_calls,
    tool_observation,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.config import (
    QASettings,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.models import (
    AssessDecision,
    ReadDecision,
    SearchDecision,
    SearchQuery,
    TaskItem,
    TaskPlan,
    WikiReadDecision,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.prompts import (
    COMMON_RULES,
    TOOL_ACTION_REMINDER,
    TOOLS,
    encode,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.tool_calling import (
    ReadWindow,
    ToolExecutionResult,
    ToolSession,
    accept_search_query,
    build_policy_catalog,
    build_tool_context,
    matching_search_batches,
    new_tool_session,
    search_completed,
    select_handoff_chunks,
    select_snapshot_entries,
)
from intelligent_reimbursement_assitant.policy_qa_service.ports.repository import QARepository


class ToolExecutor(Protocol):
    async def __call__(
        self,
        request: ToolInput,
        ctx: ExecutionContext,
        *,
        check_eligibility: Callable[[], Awaitable[bool]],
        report_progress: ReportProgress | None = None,
    ) -> ToolOutput: ...


class RankingRefresher(Protocol):
    async def __call__(
        self,
        state: QAState,
        item: TaskItem,
        entries: list[SnapshotEntry],
        ctx: ExecutionContext,
        *,
        report_progress: ReportProgress | None = None,
        check_eligibility: Callable[[], Awaitable[bool]] | None = None,
    ) -> QAState: ...


async def propose_tools(
    messages: list[dict[str, JsonValue]],
    tools: list[dict[str, JsonValue]],
    chunk_bindings: dict[str, str],
    policy_bindings: dict[str, str],
    ctx: ExecutionContext,
    *,
    gateway: ModelGateway,
    repository: QARepository,
    state_service: ExecutionStateService,
    model_settings: ModelSettings,
    count_tokens: Callable[[str], int],
    on_response: Callable[[], None],
    check_eligibility: Callable[[], Awaitable[bool]],
    report_progress: ReportProgress | None = None,
    wiki_bindings: dict[str, str] | None = None,
) -> ToolResponse:
    return await call_tools(
        messages=messages,
        tools=tools,
        chunk_bindings=chunk_bindings,
        policy_bindings=policy_bindings,
        wiki_bindings=wiki_bindings,
        gateway=gateway,
        repository=repository,
        state_service=state_service,
        ctx=ctx,
        settings=model_settings,
        count_tokens=count_tokens,
        on_response=on_response,
        check_eligibility=check_eligibility,
        before_call=partial(report_progress, ProgressStage.SELECTING_TOOLS)
        if report_progress
        else None,
    )


def prepare_tool_step(
    state: QAState,
    item_id: str,
    call: NativeCall,
    settings: QASettings,
) -> QAState | None:
    if any(
        p["item_id"] == item_id and p["tool_call_id"] == call.id
        for p in state["pending_tool_executions"]
    ):
        return state
    args = json.loads(call.function.arguments)
    session = state["tool_sessions"][item_id]
    step_cost = int(
        call.function.name != "wiki_read"
        or session["wiki_bindings"][args["page_ref"]] not in session["wiki_free_source_page_ids"]
    )
    if state["tool_steps"] + step_cost > settings.tool_step_limit:
        return None
    if call.function.name == "wiki_read":
        decision = WikiReadDecision(
            action="wiki", item_id=item_id, page_id=session["wiki_bindings"][args["page_ref"]]
        )
    elif call.function.name == "chunk_read":
        decision = ReadDecision(
            action="read",
            item_id=item_id,
            chunk_ids=[session["chunk_bindings"][ref] for ref in args["chunk_refs"]],
        )
    else:
        mode: Literal["keyword", "semantic"] = (
            "keyword" if call.function.name == "keyword_search" else "semantic"
        )
        item = next(item for item in cast(TaskPlan, state["plan"]).items if item.item_id == item_id)
        entries = select_snapshot_entries(item, state["input"].snapshot)
        documents = (
            [UUID(session["policy_bindings"][ref]) for ref in args["policy_refs"]]
            if args["policy_refs"]
            else list({entry.source.document_id for entry in entries})
        )
        queries, query = accept_search_query(
            state["search_queries"],
            item_id,
            mode,
            args["query"],
            documents,
            state["retrieval_round"],
        )
        valid_ids = {q.query_id for q in queries}
        state = {
            **state,
            "search_queries": queries,
            "search_batches": [b for b in state["search_batches"] if b.query_id in valid_ids],
            "ranked_chunk_ids": {
                key: ids for key, ids in state["ranked_chunk_ids"].items() if key != item_id
            },
        }
        decision = SearchDecision(action="search", query_id=query.query_id)
    return {
        **state,
        "pending_tool_executions": [
            *state["pending_tool_executions"],
            {"item_id": item_id, "tool_call_id": call.id, "decision": decision},
        ],
        "tool_steps": state["tool_steps"] + step_cost,
    }


def _append(state: QAState, item_id: str, *messages: dict[str, JsonValue]) -> QAState:
    session = state["tool_sessions"][item_id]
    return {
        **state,
        "tool_sessions": {
            **state["tool_sessions"],
            item_id: {**session, "messages": [*session["messages"], *messages]},
        },
    }


def _retrieval_closed(session: ToolSession, retrieval_round: int) -> bool:
    if session["retrieval_round"] != retrieval_round:
        return False
    finished = {
        call["id"]
        for message in session["messages"]
        if message["role"] == "assistant"
        for call in cast(list[dict], message.get("tool_calls", []))
        if call["function"]["name"] == "finish_retrieval"
    }
    for message in reversed(session["messages"]):
        if message["role"] == "tool":
            value = json.loads(cast(str, message["content"]))
            if message["tool_call_id"] in finished and value.get("status") == "completed":
                return False
            if value.get("retrieval_closed"):
                return True
    return False


def _reuse_read(
    state: QAState, item_id: str, decision: ReadDecision, entries: list[SnapshotEntry], radius: int
) -> ToolExecutionResult | None:
    generations = {entry.generation_id for entry in entries}
    seeds = decision.chunk_ids
    for window in state["tool_sessions"][item_id]["read_windows"]:
        if (
            set(window["generation_ids"]) == {str(key) for key in generations}
            and window["neighbor_radius"] == radius
            and set(window["seed_ids"]) == set(seeds)
        ):
            return {"status": "completed", "returned_chunk_ids": window["returned_ids"]}
    if not set(seeds) <= state["evidence_chunks"].keys():
        return None
    chunks = [
        chunk for chunk in state["evidence_chunks"].values() if chunk.generation_id in generations
    ]
    by_position = {(chunk.generation_id, chunk.ordinal): chunk for chunk in chunks}
    selected: dict[str, object] = {}
    for key in seeds:
        seed = state["evidence_chunks"][key]
        for ordinal in range(max(0, seed.ordinal - radius), seed.ordinal + radius + 1):
            chunk = by_position.get((seed.generation_id, ordinal))
            if chunk is None:
                return None
            selected[chunk.chunk_id] = chunk
    return {"status": "completed", "returned_chunk_ids": list(selected)}


def _model_observation(result: ToolExecutionResult) -> dict[str, object]:
    observation: dict[str, object] = {"status": result["status"]}
    for key in (
        "hit_count",
        "reason",
        "retrieval_closed",
        "remaining_chunk_slots",
        "additional_chunk_count",
        "tool_response_error",
        "wiki_page",
    ):
        if key in result:
            observation[key] = cast(dict, result)[key]
    return observation


async def coordinate_tools(
    state: QAState,
    items: list[TaskItem],
    ctx: ExecutionContext,
    *,
    gateway: ModelGateway,
    repository: QARepository,
    state_service: ExecutionStateService,
    settings: QASettings,
    model_settings: ModelSettings,
    count_tokens: Callable[[str], int],
    execute_tool: ToolExecutor,
    refresh_item_ranking: RankingRefresher,
    neighbor_radius: int,
    progress: ProgressFactory | None = None,
    wiki_reader: WikiReaderPort | None = None,
    io_pool: asyncio.Semaphore | None = None,
) -> AsyncGenerator[QAState]:
    """Yield response, admission and completion separately; only this task writes state."""
    plan = cast(TaskPlan, state["plan"])
    if isinstance(state["decision"], AssessDecision):
        state = {**state, "decision": None}
    io_pool = io_pool or asyncio.Semaphore(settings.tool_concurrency)
    ready: asyncio.Queue[tuple[TaskItem | None, asyncio.Task]] = asyncio.Queue()
    active: dict[str, asyncio.Task[ToolResponse]] = {}
    issued: list[asyncio.Task[Any]] = []
    operations: dict[tuple[str, str], asyncio.Task[ToolOutput]] = {}
    rankings: dict[str, asyncio.Task[QAState]] = {}
    deferred: dict[tuple[str, str], ToolExecutionResult] = {}
    ended: set[str] = set()
    capacity_order = [
        (p["item_id"], p["tool_call_id"])
        for p in state["pending_tool_executions"]
        if isinstance(p["decision"], ReadDecision)
    ]
    manifests: dict[str, dict] = {}
    budget_exhausted = False
    stopping = False

    def failure() -> BaseException | None:
        for task in issued:
            if task.done():
                if task.cancelled():
                    return asyncio.CancelledError()
                if task.exception() is not None:
                    return task.exception()
        return None

    def ensure_no_failure() -> None:
        error = failure()
        if error is not None:
            raise error from None

    async def guard() -> bool:
        nonlocal budget_exhausted
        ensure_no_failure()
        if stopping or budget_exhausted:
            return False
        allowed = await check_execution(repository, state_service, ctx, defer_budget=True)
        if not allowed:
            budget_exhausted = True
        return allowed

    async def eligible() -> None:
        if not await guard():
            raise asyncio.CancelledError("Execution no longer permitted")

    async def context_for(
        item: TaskItem, entries: list[SnapshotEntry], session: ToolSession, retrieval_open: bool
    ):
        session = cast(
            ToolSession,
            {
                **session,
                "wiki_bindings": session.get("wiki_bindings", {}),
                "wiki_read_page_ids": session.get("wiki_read_page_ids", []),
            },
        )
        wiki_context = None
        if wiki_reader is not None and state["input"].wiki_ref is not None:
            page_ref = state["input"].wiki_ref
            load_page = wiki_reader.load_page
            try:
                page = await run_io(
                    lambda: asyncio.to_thread(
                        load_page,
                        page_ref,
                        state["input"].snapshot.kb_id,
                        "overview",
                        entries,
                    ),
                    io_pool,
                    guard,
                )
                manifests[item.item_id] = page["page_members"]
                projection, wiki_bindings, chunk_bindings = wiki_reader.project_page(
                    page,
                    session["wiki_bindings"],
                    session["chunk_bindings"],
                    build_policy_catalog(entries, session["policy_bindings"])[1],
                )
                session = cast(
                    ToolSession,
                    {**session, "wiki_bindings": wiki_bindings, "chunk_bindings": chunk_bindings},
                )
                wiki_context = projection
                # Full navigation stays in native observations; the directory keeps references.
                for message in session["messages"]:
                    if message["role"] != "tool":
                        continue
                    observation = json.loads(cast(str, message["content"]))
                    page = observation.get("wiki_page")
                    if page is not None:
                        wiki_context["directory"].update(
                            {
                                ref: {
                                    key: value for key, value in entry.items() if key != "sources"
                                }
                                for ref, entry in page["directory"].items()
                            }
                        )
                        wiki_context["chunks"].update(
                            {
                                ref: {
                                    "content_kind": "wiki_reference",
                                    "page_ref": page["page_ref"],
                                }
                                for ref in page["chunks"]
                            }
                        )
            except ExecutionError as error:
                if error.code not in (Code.WIKI_SOURCE_UNAVAILABLE, Code.WIKI_ARTIFACT_ERROR):
                    raise
                logging.getLogger(__name__).warning(
                    "Wiki navigation unavailable job_id=%s ref=%s code=%s",
                    ctx.job_id,
                    state["input"].wiki_ref.key,
                    error.code,
                )
                wiki_context = {"unavailable_reason": error.code}
        return build_tool_context(
            item,
            plan,
            state["search_queries"],
            state["search_batches"],
            state["ranked_chunk_ids"],
            state["evidence_chunks"],
            state["evidence_ids_by_item"].get(item.item_id, []),
            entries,
            session=session,
            assessments=state["assessments"],
            chunk_history=state["chunk_history_by_item"].get(item.item_id, {}),
            retrieval_round=state["retrieval_round"],
            remaining_tool_steps=settings.tool_step_limit - state["tool_steps"],
            preview_limit=settings.candidate_preview_limit,
            remaining_chunk_slots=settings.read_chunk_limit - len(state["evidence_chunks"]),
            neighbor_radius=neighbor_radius,
            retrieval_open=retrieval_open,
            fact_values=state["fact_values"],
            wiki_context=wiki_context,
        )

    async def retrieval_open_for(session: ToolSession) -> bool:
        return (
            (
                state["tool_steps"] < settings.tool_step_limit
                or bool(session["wiki_free_source_page_ids"])
            )
            and await retrieval_allowed(repository, ctx, settings)
            and not _retrieval_closed(session, state["retrieval_round"])
        )

    async def launch(item: TaskItem) -> None:
        if item.item_id in active or item.item_id in ended:
            return
        ensure_no_failure()
        entries = select_snapshot_entries(item, state["input"].snapshot)
        session = state["tool_sessions"].get(item.item_id)
        if session is None:
            session = new_tool_session(state["retrieval_round"])
        if session["handoff_round"] == state["retrieval_round"]:
            ended.add(item.item_id)
            return
        retrieval_open = await retrieval_open_for(session)
        context, tools, request_session = await context_for(item, entries, session, retrieval_open)
        wiki_context = context.get("wiki")
        previous = session["messages"]
        if consecutive_tool_response_errors(previous) > settings.structure_repair_limit:
            if (
                previous[-1]["role"] == "tool"
                and json.loads(cast(str, previous[-1]["content"])).get("reason")
                == "read_limit_reached"
            ):
                raise ExecutionError(
                    Code.QA_EVIDENCE_CAPACITY_EXCEEDED,
                    "PERMANENT",
                    "Evidence handoff still exceeds cache capacity after bounded repair",
                )
            raise ExecutionError(
                Code.QA_MODEL_INVALID_RESPONSE, "PERMANENT", "Invalid native tool arguments"
            )
        user: dict[str, JsonValue] = {"role": "user", "content": encode(context)}
        # Keep native history intact and put the current task after prior handoffs.
        messages = [{"role": "system", "content": COMMON_RULES + TOOLS}, *previous, user]
        if previous and previous[-1]["role"] == "assistant" and not previous[-1].get("tool_calls"):
            messages.append(
                {
                    "role": "user",
                    "content": TOOL_ACTION_REMINDER,
                }
            )
        if (
            wiki_context is not None
            and count_tokens(encode({"messages": messages, "tools": tools}))
            + model_settings.max_output_tokens
            > model_settings.context_window_tokens
        ):
            context.pop("wiki", None)
            context["wiki_unavailable_reason"] = "context_capacity"
            tools = [tool for tool in tools if cast(dict, tool["function"])["name"] != "wiki_read"]
            user["content"] = encode(context)
        notified = False

        def notify_response():
            nonlocal notified
            if not notified:
                notified = True
                ready.put_nowait((item, task))

        task = asyncio.create_task(
            propose_tools(
                cast(list[dict[str, JsonValue]], messages),
                tools,
                {
                    ref: key
                    for ref, key in request_session["chunk_bindings"].items()
                    if ref in cast(dict, context["chunks"])
                },
                {
                    ref: key
                    for ref, key in request_session["policy_bindings"].items()
                    if ref in cast(dict, context["policies"])
                },
                ctx,
                gateway=gateway,
                repository=repository,
                state_service=state_service,
                model_settings=model_settings,
                count_tokens=count_tokens,
                on_response=notify_response,
                check_eligibility=guard,
                wiki_bindings=request_session.get("wiki_bindings", {}),
                report_progress=progress(ctx, item.item_id, None) if progress else None,
            )
        )
        # Connection failures/eligibility exits also wake the coordinator.
        task.add_done_callback(lambda _: notify_response())
        active[item.item_id] = task
        issued.append(task)
        request_sessions[task] = (
            {**request_session, "retrieval_round": state["retrieval_round"]},
            [request_session["chunk_bindings"][ref] for ref in cast(dict, context["chunks"])],
        )

    request_sessions: dict[asyncio.Task[ToolResponse], tuple[ToolSession, list[str]]] = {}

    def watch(task: asyncio.Task[Any]) -> None:
        issued.append(task)
        task.add_done_callback(lambda done: ready.put_nowait((None, done)))

    def make_request(pending) -> ToolInput:
        decision = pending["decision"]
        item = next(item for item in items if item.item_id == pending["item_id"])
        available = {c.chunk_id: c for batch in state["search_batches"] for c in batch.chunks}
        available.update(state["evidence_chunks"])
        ids = decision.chunk_ids if isinstance(decision, ReadDecision) else []
        return ToolInput(
            decision=decision,
            entries=select_snapshot_entries(item, state["input"].snapshot),
            kb_id=state["input"].snapshot.kb_id,
            wiki_ref=state["input"].wiki_ref,
            query=next(
                (
                    q
                    for q in state["search_queries"]
                    if isinstance(decision, SearchDecision) and q.query_id == decision.query_id
                ),
                None,
            ),
            seeds=[available[key] for key in ids if key in available],
            wiki_chunk_ids=[key for key in ids if key not in available],
        )

    async def perform(pending) -> ToolOutput:
        request = make_request(pending)
        decision = request.decision
        item_id = pending["item_id"]
        if isinstance(decision, SearchDecision):
            query = cast(SearchQuery, request.query)
            if search_completed(state["search_batches"], query, request.entries):
                batches = matching_search_batches(state["search_batches"], query, request.entries)
                return ToolOutput(
                    {
                        "status": "completed",
                        "reason": "reused",
                        "hit_count": len({c.chunk_id for batch in batches for c in batch.chunks}),
                    }
                )
            if not await retrieval_allowed(repository, ctx, settings):
                return ToolOutput(
                    {
                        "status": "not_executed",
                        "reason": "retrieval_budget_reached",
                        "retrieval_closed": True,
                    }
                )
        if isinstance(decision, ReadDecision):
            observation = _reuse_read(state, item_id, decision, request.entries, neighbor_radius)
            if observation is not None:
                return ToolOutput({**observation, "reason": "reused", "retrieval_closed": True})
        return await execute_tool(
            request,
            ctx,
            check_eligibility=guard,
            report_progress=progress(ctx, item_id, pending["tool_call_id"]) if progress else None,
        )

    async def admit(item: TaskItem) -> AsyncIterator[QAState]:
        nonlocal state
        session = state["tool_sessions"].get(item.item_id)
        if session is None:
            return
        if (
            item.item_id not in manifests
            and wiki_reader is not None
            and any(
                call.function.name == "wiki_read"
                for call in pending_tool_calls(session["messages"])
            )
        ):
            manifests[item.item_id] = {}
            await context_for(
                item,
                select_snapshot_entries(item, state["input"].snapshot),
                session,
                await retrieval_open_for(session),
            )
        for call in pending_tool_calls(session["messages"]):
            key = (item.item_id, call.id)
            if key in deferred:
                continue
            if any(
                p["item_id"] == item.item_id and p["tool_call_id"] == call.id
                for p in state["pending_tool_executions"]
            ):
                continue
            if call.function.name == "finish_retrieval":
                if key not in capacity_order:
                    capacity_order.append(key)
                continue
            if _retrieval_closed(state["tool_sessions"][item.item_id], state["retrieval_round"]):
                deferred[key] = {
                    "status": "not_executed",
                    "reason": "retrieval_closed",
                    "retrieval_closed": True,
                }
                continue
            if call.function.name == "wiki_read":
                args = json.loads(call.function.arguments)
                page_id = session["wiki_bindings"][args["page_ref"]]
                previous = [
                    p["decision"]
                    for p in state["pending_tool_executions"]
                    if p["item_id"] == item.item_id and isinstance(p["decision"], WikiReadDecision)
                ]
                if any(
                    p.page_id == page_id
                    or page_id in manifests.get(item.item_id, {}).get(p.page_id, [])
                    for p in previous
                ):
                    continue
            admitted = prepare_tool_step(state, item.item_id, call, settings)
            if admitted is None:
                deferred[key] = {"status": "not_executed", "reason": "tool_limit_reached"}
                continue
            state = admitted
            if isinstance(state["pending_tool_executions"][-1]["decision"], ReadDecision):
                capacity_order.append(key)
            yield state
            # The iterator resumes only after the native Saver acknowledged admission.
            pending = state["pending_tool_executions"][-1]
            operations[key] = asyncio.create_task(perform(pending))
            watch(operations[key])

    async def handoff(item: TaskItem, call: NativeCall) -> AsyncIterator[QAState]:
        nonlocal state
        entries = select_snapshot_entries(item, state["input"].snapshot)
        await eligible()
        args = json.loads(call.function.arguments)
        session = state["tool_sessions"][item.item_id]
        try:
            chunks = select_handoff_chunks(
                args["chunk_refs"],
                session,
                [
                    batch
                    for query in state["search_queries"]
                    if query.item_id == item.item_id
                    and query.retrieval_round == state["retrieval_round"]
                    for batch in matching_search_batches(state["search_batches"], query, entries)
                ],
                state["evidence_chunks"],
                state["evidence_ids_by_item"].get(item.item_id, []),
            )
        except ValueError:
            state = _append(
                state,
                item.item_id,
                tool_observation(
                    call.id,
                    {
                        "status": "not_executed",
                        "reason": "original_content_required",
                        "tool_response_error": "Read Wiki chunk references or search original content before handoff",
                    },
                ),
            )
            yield state
            return
        ids = [chunk.chunk_id for chunk in chunks]
        additional = set(ids) - state["evidence_chunks"].keys()
        remaining = settings.read_chunk_limit - len(state["evidence_chunks"])
        if len(additional) > remaining:
            state = _append(
                state,
                item.item_id,
                tool_observation(
                    call.id,
                    {
                        "status": "not_executed",
                        "reason": "read_limit_reached",
                        "remaining_chunk_slots": remaining,
                        "additional_chunk_count": len(additional),
                        "uncached_chunk_refs": [
                            ref
                            for ref in args["chunk_refs"]
                            if session["chunk_bindings"][ref] in additional
                        ],
                        "cached_chunk_refs": [
                            ref
                            for ref in args["chunk_refs"]
                            if session["chunk_bindings"][ref] not in additional
                        ],
                        "tool_response_error": "Reduce chunk_refs to fit capacity or adopt already cached chunks",
                    },
                ),
            )
            yield state
            return
        previous_ids = state["evidence_ids_by_item"].get(item.item_id, [])
        state = {
            **state,
            "evidence_chunks": {
                **state["evidence_chunks"],
                **{chunk.chunk_id: chunk for chunk in chunks},
            },
            "evidence_ids_by_item": {
                **state["evidence_ids_by_item"],
                item.item_id: list(dict.fromkeys([*previous_ids, *ids])),
            },
            "retrieval_notes": {
                **state["retrieval_notes"],
                item.item_id: args["explanation"],
            },
            "tool_sessions": {
                **state["tool_sessions"],
                item.item_id: {**session, "handoff_round": state["retrieval_round"]},
            },
        }
        if set(ids) - set(previous_ids):
            state = {
                **state,
                "assessments": [a for a in state["assessments"] if a.item_id != item.item_id],
                "answer_sections": [
                    d for d in state["answer_sections"] if d.item_id != item.item_id
                ],
                "result": None,
            }
        state = _append(state, item.item_id, tool_observation(call.id, {"status": "completed"}))
        ended.add(item.item_id)
        yield state
        return

    async def commit(item: TaskItem, pending, output: ToolOutput) -> None:
        nonlocal state
        decision = pending["decision"]
        entries = select_snapshot_entries(item, state["input"].snapshot)
        call_id = pending["tool_call_id"]
        observation: ToolExecutionResult = output.observation
        if output.batches:
            merged = {_batch_key(b): b for b in [*state["search_batches"], *output.batches]}
            state = {
                **state,
                "search_batches": [merged[k] for k in sorted(merged)],
                "ranked_chunk_ids": {
                    k: v for k, v in state["ranked_chunk_ids"].items() if k != item.item_id
                },
            }
        if isinstance(decision, ReadDecision) and output.chunks:
            additional = {c.chunk_id for c in output.chunks} - state["evidence_chunks"].keys()
            remaining = settings.read_chunk_limit - len(state["evidence_chunks"])
            if len(additional) > remaining:
                observation = {
                    "status": "not_executed",
                    "reason": "read_limit_reached",
                    "remaining_chunk_slots": remaining,
                    "additional_chunk_count": len(additional),
                }
            else:
                state = {
                    **state,
                    "evidence_chunks": {
                        **state["evidence_chunks"],
                        **{c.chunk_id: c for c in output.chunks},
                    },
                }
                observation = {
                    **observation,
                    "returned_chunk_ids": [c.chunk_id for c in output.chunks],
                }
        elif (
            isinstance(decision, ReadDecision)
            and observation["status"] == "completed"
            and "returned_chunk_ids" not in observation
        ):
            observation = {**observation, "returned_chunk_ids": []}
        if output.page is not None:
            session = state["tool_sessions"][item.item_id]
            projection, wiki, chunks = cast(WikiReaderPort, wiki_reader).project_page(
                output.page,
                session["wiki_bindings"],
                session["chunk_bindings"],
                build_policy_catalog(entries, session["policy_bindings"])[1],
            )
            observation = {"status": "completed", "wiki_page": projection}
            read_pages = list(
                dict.fromkeys(
                    [*session["wiki_read_page_ids"], cast(WikiReadDecision, decision).page_id]
                )
            )
            free = [*session["wiki_free_source_page_ids"]]
            if projection["kind"] == "synthesis":
                free.extend(wiki[ref] for ref in projection["directory"])
            trial_session = cast(
                ToolSession,
                {
                    **session,
                    "wiki_bindings": wiki,
                    "chunk_bindings": chunks,
                    "wiki_read_page_ids": read_pages,
                    "wiki_free_source_page_ids": list(
                        dict.fromkeys(k for k in free if k not in read_pages)
                    ),
                    "messages": [
                        *session["messages"],
                        tool_observation(call_id, _model_observation(observation)),
                    ],
                },
            )
            context, tools, _ = await context_for(
                item, entries, trial_session, await retrieval_open_for(trial_session)
            )
            messages = [
                {"role": "system", "content": COMMON_RULES + TOOLS},
                *trial_session["messages"],
                {"role": "user", "content": encode(context)},
            ]
            if (
                count_tokens(encode({"messages": messages, "tools": tools}))
                + model_settings.max_output_tokens
                > model_settings.context_window_tokens
            ):
                observation = {
                    "status": "not_executed",
                    "reason": "wiki_page_exceeds_context_capacity",
                }
            else:
                state = {
                    **state,
                    "tool_sessions": {
                        **state["tool_sessions"],
                        item.item_id: {**trial_session, "messages": session["messages"]},
                    },
                }
        if isinstance(decision, ReadDecision) and observation["status"] == "completed":
            ids = cast(dict, observation)["returned_chunk_ids"]
            session = state["tool_sessions"][item.item_id]
            window: ReadWindow = {
                "seed_ids": decision.chunk_ids,
                "generation_ids": sorted(str(entry.generation_id) for entry in entries),
                "neighbor_radius": neighbor_radius,
                "returned_ids": ids,
            }
            history = dict(state["chunk_history_by_item"].get(item.item_id, {}))
            for key in ids:
                history[key] = {
                    "shown_rounds": history[key]["shown_rounds"] if key in history else [],
                    "read": True,
                }
            state = {
                **state,
                "chunk_history_by_item": {
                    **state["chunk_history_by_item"],
                    item.item_id: history,
                },
                "tool_sessions": {
                    **state["tool_sessions"],
                    item.item_id: {
                        **session,
                        "read_windows": [*session["read_windows"], window],
                    },
                },
            }
        state = _append(
            state, item.item_id, tool_observation(call_id, _model_observation(observation))
        )
        state = {
            **state,
            "pending_tool_executions": [
                p
                for p in state["pending_tool_executions"]
                if (p["item_id"], p["tool_call_id"]) != (item.item_id, call_id)
            ],
        }

    async def drain() -> AsyncIterator[QAState]:
        nonlocal state
        for item in items:
            while True:
                session = state["tool_sessions"].get(item.item_id)
                calls = pending_tool_calls(session["messages"]) if session else []
                if not calls:
                    break
                call = calls[0]
                key = (item.item_id, call.id)
                if key in deferred:
                    state = _append(
                        state, item.item_id, tool_observation(call.id, deferred.pop(key))
                    )
                    yield state
                    continue
                if call.function.name == "finish_retrieval":
                    if not capacity_order or capacity_order[0] != key:
                        break
                    async for update in handoff(item, call):
                        yield update
                    capacity_order.pop(0)
                    continue
                pending = next(
                    (
                        p
                        for p in state["pending_tool_executions"]
                        if (p["item_id"], p["tool_call_id"]) == key
                    ),
                    None,
                )
                task = operations.get(key)
                if pending is None or task is None or not task.done():
                    break
                if isinstance(pending["decision"], ReadDecision):
                    if capacity_order[0] != key:
                        break
                    capacity_order.pop(0)
                await commit(item, pending, task.result())
                del operations[key]
                yield state
            if item.item_id in rankings and rankings[item.item_id].done():
                result = rankings.pop(item.item_id).result()
                state = {
                    **state,
                    "ranked_chunk_ids": {
                        **state["ranked_chunk_ids"],
                        item.item_id: result["ranked_chunk_ids"][item.item_id],
                    },
                }
                yield state

    async def advance(item: TaskItem) -> None:
        if item.item_id in active or item.item_id in rankings or item.item_id in ended:
            return
        session = state["tool_sessions"].get(item.item_id)
        if session is not None and pending_tool_calls(session["messages"]):
            return
        entries = select_snapshot_entries(item, state["input"].snapshot)
        batches = [
            b
            for q in state["search_queries"]
            if q.item_id == item.item_id and q.retrieval_round == state["retrieval_round"]
            for b in matching_search_batches(state["search_batches"], q, entries)
        ]
        if (
            batches
            and item.item_id not in state["ranked_chunk_ids"]
            and await retrieval_allowed(repository, ctx, settings)
        ):
            task = asyncio.create_task(
                refresh_item_ranking(
                    state,
                    item,
                    entries,
                    ctx,
                    check_eligibility=guard,
                    report_progress=progress(ctx, item.item_id, None) if progress else None,
                )
            )
            rankings[item.item_id] = task
            watch(task)
        else:
            await launch(item)

    try:
        # Recover paid admissions without charging again, before issuing new proposals.
        for pending in state["pending_tool_executions"]:
            key = (pending["item_id"], pending["tool_call_id"])
            operations[key] = asyncio.create_task(perform(pending))
            watch(operations[key])
        while True:
            ensure_no_failure()
            # Admissions are saved and their I/O started before draining ready products.
            progressed = False
            for item in items:
                async for update in admit(item):
                    progressed = True
                    yield update
            async for update in drain():
                progressed = True
                yield update
            # A completed dependency may make another call admissible immediately.
            blocked = any(
                pending_tool_calls(session["messages"])
                for session in state["tool_sessions"].values()
            )
            if blocked and not operations and not active and not rankings:
                continue
            for item in items:
                await advance(item)
            if not active and not operations and not rankings:
                break
            if progressed:
                continue
            item, task = await ready.get()
            if item is None:
                continue
            response = await asyncio.shield(task)
            ensure_no_failure()
            del active[item.item_id]
            request_session, shown_ids = request_sessions[task]
            history = dict(state["chunk_history_by_item"].get(item.item_id, {}))
            for key in shown_ids:
                record = history.get(key, {"shown_rounds": [], "read": False})
                history[key] = {
                    **record,
                    "shown_rounds": list(
                        dict.fromkeys([*record["shown_rounds"], request_session["retrieval_round"]])
                    ),
                }
            state = {
                **state,
                "tool_sessions": {**state["tool_sessions"], item.item_id: request_session},
                "chunk_history_by_item": {**state["chunk_history_by_item"], item.item_id: history},
            }
            observations = [
                tool_observation(call.id, {"parameter_error": response.errors[call.id]})
                for call in response.calls
                if call.id in response.errors
            ]
            state = _append(state, item.item_id, response.message, *observations)
            yield state
            if (
                response.errors
                and consecutive_tool_response_errors(request_session["messages"])
                >= settings.structure_repair_limit
            ):
                raise ExecutionError(
                    Code.QA_MODEL_INVALID_RESPONSE, "PERMANENT", "Invalid native tool arguments"
                )
        await eligible()
        state = {**state, "decision": AssessDecision(action="assess")}
        yield state
    except BaseException as caught:
        primary: BaseException = caught
        stopping = True
        try:
            await gather_settled(*issued)
        except BaseException as error:
            if isinstance(error, ExecutionError) and error.category == "OUTCOME_UNKNOWN":
                primary = error
        if budget_exhausted:
            try:
                await check_execution(repository, state_service, ctx)
            except BaseException as error:
                if not (
                    isinstance(primary, ExecutionError) and primary.category == "OUTCOME_UNKNOWN"
                ):
                    primary = error
        raise primary from None
