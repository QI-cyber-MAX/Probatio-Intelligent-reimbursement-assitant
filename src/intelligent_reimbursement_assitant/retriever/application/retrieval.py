"""Search and read only the original profiles and generations in a fixed snapshot."""

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from functools import partial
from uuid import UUID

from intelligent_reimbursement_assitant.content_index.ports.content_index import ContentIndexPort
from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    Chunk,
    SnapshotEntry,
)
from intelligent_reimbursement_assitant.errors.codes import ErrorCode as Code
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.errors.execution import (
    gather_settled,
    retry_safe_operation,
    run_io,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.events import (
    ProgressStage,
    ReportProgress,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.models import (
    SearchBatch,
    SearchRequest,
)


def _profile_generations(entries: list[SnapshotEntry]) -> dict[str, set[UUID]]:
    groups: dict[str, set[UUID]] = {}
    for entry in entries:
        groups.setdefault(entry.index_profile_id, set()).add(entry.generation_id)
    return groups


def _bind_sources(chunks: list[Chunk], entries: list[SnapshotEntry]) -> None:
    sources = {entry.generation_id: entry.source for entry in entries}
    if any(
        (chunk.document_id, chunk.source_hash)
        != (sources[chunk.generation_id].document_id, sources[chunk.generation_id].source_hash)
        for chunk in chunks
    ):
        raise ExecutionError(Code.INDEX_READ_ERROR, "PERMANENT", "Index source escaped snapshot")


class Retriever:
    def __init__(
        self, indexes: Mapping[str, ContentIndexPort], *, io_pool: asyncio.Semaphore | None = None
    ):
        self.indexes = indexes
        self.io_pool = io_pool or asyncio.Semaphore(4)

    def _index(self, profile_id: str) -> ContentIndexPort:
        try:
            return self.indexes[profile_id]
        except KeyError as error:
            raise ExecutionError(
                Code.INDEX_PROFILE_UNAVAILABLE, "PERMANENT", "Bound index profile is unavailable"
            ) from error

    async def search(
        self,
        request: SearchRequest,
        embed_query: Callable[[str, str, Callable[[], Awaitable[bool]]], Awaitable[list[float]]],
        check_execution: Callable[[], Awaitable[bool]],
        *,
        report_progress: ReportProgress | None = None,
    ) -> list[SearchBatch]:
        stopped = False

        async def eligible():
            return not stopped and await check_execution()

        async def profile_search(profile_id: str, generations: set[UUID]) -> SearchBatch:
            nonlocal stopped
            try:
                return await search_profile(profile_id, generations)
            except BaseException:
                stopped = True
                raise

        async def search_profile(profile_id: str, generations: set[UUID]) -> SearchBatch:
            index = self._index(profile_id)
            generation_ids = sorted(generations)
            if request.query.mode == "keyword":
                search = partial(
                    index.keyword_search,
                    request.query.text,
                    request.query.document_ids,
                    generation_ids,
                    request.limit,
                )
            else:
                vector = await embed_query(profile_id, request.query.text, eligible)

                search = partial(index.semantic_search, vector, generation_ids, request.limit)

            async def search_with_progress(search=search):
                if report_progress is not None:
                    await report_progress(
                        ProgressStage.KEYWORD_SEARCH
                        if request.query.mode == "keyword"
                        else ProgressStage.SEMANTIC_SEARCH
                    )
                return await search()

            chunks = await retry_safe_operation(
                lambda: run_io(search_with_progress, self.io_pool, eligible), eligible
            )
            _bind_sources(chunks, request.entries)
            return SearchBatch(
                query_id=request.query.query_id,
                mode=request.query.mode,
                index_profile_id=profile_id,
                generation_ids=generation_ids,
                chunks=chunks,
            )

        return await gather_settled(
            *(
                profile_search(profile, generations)
                for profile, generations in sorted(_profile_generations(request.entries).items())
            )
        )

    async def read_chunks(
        self,
        seeds: list[Chunk],
        entries: list[SnapshotEntry],
        neighbor_radius: int,
        check_execution: Callable[[], Awaitable[bool]],
        *,
        report_progress: ReportProgress | None = None,
    ) -> list[Chunk]:
        stopped = False

        async def eligible():
            return not stopped and await check_execution()

        async def read_profile(profile_id: str, generations: set[UUID]) -> list[Chunk]:
            nonlocal stopped
            try:
                return await profile_read(profile_id, generations)
            except BaseException:
                stopped = True
                raise

        async def profile_read(profile_id: str, generations: set[UUID]) -> list[Chunk]:
            ids = [seed.chunk_id for seed in seeds if seed.generation_id in generations]
            if not ids:
                return []
            index = self._index(profile_id)
            generation_ids = sorted(generations)

            async def read_with_progress(index=index, ids=ids, generation_ids=generation_ids):
                if report_progress is not None:
                    await report_progress(ProgressStage.CHUNK_READ)
                return await index.chunk_read(ids, generation_ids, neighbor_radius)

            chunks = await retry_safe_operation(
                lambda: run_io(read_with_progress, self.io_pool, eligible), eligible
            )
            _bind_sources(chunks, entries)
            return chunks

        groups = await gather_settled(
            *(
                read_profile(profile, generations)
                for profile, generations in sorted(_profile_generations(entries).items())
            )
        )
        selected = {c.chunk_id: c for chunks in groups for c in chunks}
        return sorted(selected.values(), key=lambda c: (c.generation_id, c.ordinal, c.chunk_id))
