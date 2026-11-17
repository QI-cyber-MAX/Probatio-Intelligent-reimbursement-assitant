"""Tool execution, per-item fusion/reranking and complete read-window admission."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from functools import partial
from typing import cast
from uuid import UUID

from intelligent_reimbursement_assitant.contracts.models import ArtifactRef
from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    Chunk,
    SnapshotEntry,
)
from intelligent_reimbursement_assitant.embedding.ports.embedding import EmbeddingPort
from intelligent_reimbursement_assitant.errors.codes import ErrorCode as Code
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.errors.execution import gather_settled, run_io
from intelligent_reimbursement_assitant.execution_state_service.application.service import (
    ExecutionStateService,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.events import (
    ProgressStage,
    ReportProgress,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionContext,
)
from intelligent_reimbursement_assitant.knowledge_wiki.ports.reading import WikiReaderPort
from intelligent_reimbursement_assitant.policy_qa_graph.domain.state import QAState
from intelligent_reimbursement_assitant.policy_qa_service.application.model_call import (
    call_model,
    check_execution,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.config import (
    QASettings,
    retrieval_budget_allowed,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.model_inputs import (
    fact_context,
    period_context,
    task_context,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.models import (
    ReadDecision,
    SearchBatch,
    SearchDecision,
    SearchQuery,
    SearchRequest,
    TaskItem,
    TaskPlan,
    WikiReadDecision,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.prompts import encode
from intelligent_reimbursement_assitant.policy_qa_service.domain.tool_calling import (
    ToolExecutionResult,
    matching_search_batches,
    new_tool_session,
    query_entries,
    select_retrieval_items,
)
from intelligent_reimbursement_assitant.policy_qa_service.ports.repository import QARepository
from intelligent_reimbursement_assitant.policy_qa_service.ports.reranker import RerankerPort
from intelligent_reimbursement_assitant.retriever.application.retrieval import Retriever
from intelligent_reimbursement_assitant.retriever.domain.ranking import fuse_rankings


def _batch_key(batch: SearchBatch) -> tuple:
    return batch.query_id, batch.mode, batch.index_profile_id, tuple(sorted(batch.generation_ids))


async def rank_item_candidates(
    question: str,
    batches: list[SearchBatch],
    ctx: ExecutionContext,
    *,
    settings: QASettings,
    reranker: RerankerPort,
    repository: QARepository,
    state_service: ExecutionStateService,
    report_progress: ReportProgress | None = None,
    check_eligibility: Callable[[], Awaitable[bool]] | None = None,
) -> list[str]:
    batches = sorted(batches, key=_batch_key)
    chunks = {chunk.chunk_id: chunk for batch in batches for chunk in batch.chunks}
    fused = fuse_rankings([[chunk.chunk_id for chunk in batch.chunks] for batch in batches])
    candidates = fused[: settings.rerank_candidate_limit]
    if len(candidates) < 2:
        return candidates
    eligible = check_eligibility or partial(check_execution, repository, state_service, ctx)
    raw = await call_model(
        operation=lambda: reranker.rerank(
            question, [chunks[key].retrieval_text for key in candidates], check_execution=eligible
        ),
        repository=repository,
        state_service=state_service,
        ctx=ctx,
        unknown_code=Code.RERANK_OUTCOME_UNKNOWN,
        check_eligibility=eligible,
        before_call=partial(report_progress, ProgressStage.RERANKING) if report_progress else None,
    )
    return [candidates[position] for position in reranker.parse_ranking(raw, len(candidates))]


async def refresh_item_ranking(
    state: QAState,
    item: TaskItem,
    entries: list[SnapshotEntry],
    ctx: ExecutionContext,
    *,
    settings: QASettings,
    reranker: RerankerPort,
    repository: QARepository,
    state_service: ExecutionStateService,
    report_progress: ReportProgress | None = None,
    check_eligibility: Callable[[], Awaitable[bool]] | None = None,
) -> QAState:
    if item.item_id in state["ranked_chunk_ids"]:
        return state
    batches = _item_batches(state, item, entries)
    if not batches:
        return state
    ranked = await rank_item_candidates(
        encode(
            {
                "task": task_context(item),
                "period": period_context(item),
                "facts": [
                    fact_context(fact, state["fact_values"])
                    for fact in cast(TaskPlan, state["plan"]).facts
                    if fact.fact_id in item.fact_ids
                ],
            }
        ),
        batches,
        ctx,
        settings=settings,
        reranker=reranker,
        repository=repository,
        state_service=state_service,
        report_progress=report_progress,
        check_eligibility=check_eligibility,
    )
    return {**state, "ranked_chunk_ids": {**state["ranked_chunk_ids"], item.item_id: ranked}}


@dataclass(frozen=True)
class ToolInput:
    decision: SearchDecision | ReadDecision | WikiReadDecision
    entries: list[SnapshotEntry]
    kb_id: UUID
    wiki_ref: ArtifactRef | None = None
    query: SearchQuery | None = None
    seeds: list[Chunk] = field(default_factory=list)
    wiki_chunk_ids: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ToolOutput:
    observation: ToolExecutionResult
    batches: list[SearchBatch] = field(default_factory=list)
    chunks: list[Chunk] = field(default_factory=list)
    page: dict | None = None


async def execute_tool(
    request: ToolInput,
    ctx: ExecutionContext,
    *,
    retriever: Retriever,
    embeddings: Mapping[str, EmbeddingPort],
    settings: QASettings,
    repository: QARepository,
    state_service: ExecutionStateService,
    check_eligibility: Callable[[], Awaitable[bool]],
    io_pool: asyncio.Semaphore,
    report_progress: ReportProgress | None = None,
    wiki_reader: WikiReaderPort | None = None,
) -> ToolOutput:
    decision, entries = request.decision, request.entries
    if isinstance(decision, WikiReadDecision):
        if wiki_reader is None or request.wiki_ref is None:
            return ToolOutput({"status": "not_executed", "reason": "wiki_unavailable"})
        page_ref = request.wiki_ref
        load_page = wiki_reader.load_page
        try:
            page = await run_io(
                lambda: asyncio.to_thread(
                    load_page,
                    page_ref,
                    request.kb_id,
                    decision.page_id,
                    entries,
                ),
                io_pool,
                check_eligibility,
            )
        except ExecutionError as error:
            if error.code not in (Code.WIKI_SOURCE_UNAVAILABLE, Code.WIKI_ARTIFACT_ERROR):
                raise
            return ToolOutput({"status": "not_executed", "reason": error.code})
        return ToolOutput({"status": "completed"}, page=page)

    if isinstance(decision, SearchDecision):
        query = cast(SearchQuery, request.query)

        async def embed(
            profile_id: str, text: str, eligible: Callable[[], Awaitable[bool]]
        ) -> list[float]:
            try:
                embedding = embeddings[profile_id]
            except KeyError as error:
                raise ExecutionError(
                    Code.INDEX_PROFILE_UNAVAILABLE, "PERMANENT", "Bound embedding is unavailable"
                ) from error
            return await call_model(
                operation=lambda: run_io(lambda: embedding.embed_query(text), io_pool, eligible),
                repository=repository,
                state_service=state_service,
                ctx=ctx,
                check_eligibility=eligible,
                before_call=partial(report_progress, ProgressStage.QUERY_EMBEDDING)
                if report_progress
                else None,
            )

        if not query_entries(query, entries):
            return ToolOutput({"status": "not_executed", "reason": "scope_unavailable"})
        batches = await retriever.search(
            SearchRequest(
                query=query,
                entries=query_entries(query, entries),
                limit=settings.search_limit_per_profile,
            ),
            embed,
            check_eligibility,
            report_progress=report_progress,
        )
        return ToolOutput(
            {
                "status": "completed",
                "hit_count": len({c.chunk_id for batch in batches for c in batch.chunks}),
            },
            batches=batches,
        )

    async def read_index() -> list[Chunk]:
        return (
            await retriever.read_chunks(
                request.seeds,
                entries,
                settings.neighbor_radius,
                check_eligibility,
                report_progress=report_progress,
            )
            if request.seeds
            else []
        )

    async def read_wiki() -> list[Chunk]:
        if not request.wiki_chunk_ids:
            return []
        if wiki_reader is None or request.wiki_ref is None:
            raise ExecutionError(
                Code.WIKI_SOURCE_UNAVAILABLE, "PERMANENT", "Wiki source unavailable"
            )
        read_source_chunks = wiki_reader.read_source_chunks
        page_ref = request.wiki_ref
        return await run_io(
            lambda: asyncio.to_thread(
                read_source_chunks,
                page_ref,
                request.kb_id,
                request.wiki_chunk_ids,
                entries,
                settings.neighbor_radius,
            ),
            io_pool,
            check_eligibility,
        )

    try:
        groups = await gather_settled(read_index(), read_wiki())
    except ExecutionError as error:
        if error.code in (Code.WIKI_SOURCE_UNAVAILABLE, Code.WIKI_ARTIFACT_ERROR):
            return ToolOutput({"status": "not_executed", "reason": error.code})
        raise
    chunks = list({c.chunk_id: c for group in groups for c in group}.values())
    return ToolOutput({"status": "completed"}, chunks=chunks)


def _item_batches(
    state: QAState, item: TaskItem, entries: list[SnapshotEntry]
) -> list[SearchBatch]:
    return [
        batch
        for query in state["search_queries"]
        if query.item_id == item.item_id and query.retrieval_round == state["retrieval_round"]
        for batch in matching_search_batches(state["search_batches"], query, entries)
    ]


def retrieval_round_allowed(state: QAState, settings: QASettings) -> bool:
    plan = state["plan"]
    return (
        plan is not None
        and state["retrieval_round"] < settings.retrieval_round_limit
        and state["tool_steps"] < settings.tool_step_limit
        and bool(select_retrieval_items(plan, state["assessments"]))
    )


def prepare_retrieval_round(state: QAState) -> QAState:
    plan = cast(TaskPlan, state["plan"])
    targets = {item.item_id for item in select_retrieval_items(plan, state["assessments"])}
    round_number = state["retrieval_round"] + 1
    sessions = dict(state["tool_sessions"])
    for item_id in targets:
        sessions[item_id] = {
            **sessions.get(item_id, new_tool_session(round_number)),
            "retrieval_round": round_number,
            "wiki_free_source_page_ids": [],
        }
    return {
        **state,
        "retrieval_round": round_number,
        "tool_sessions": sessions,
        "ranked_chunk_ids": {
            key: ids for key, ids in state["ranked_chunk_ids"].items() if key not in targets
        },
    }


async def retrieval_allowed(
    repository: QARepository, ctx: ExecutionContext, settings: QASettings
) -> bool:
    def load():
        with repository.session() as session:
            return repository.load_budget_used(session, ctx.job_id)

    return retrieval_budget_allowed(await asyncio.to_thread(load), settings)
