"""Elasticsearch records, recovery reads, create-only bulk writes and visibility."""

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Literal, cast
from uuid import UUID

from elastic_transport import ConnectionError, ConnectionTimeout, SerializationError
from elastic_transport.client_utils import DEFAULT, DefaultType
from elasticsearch import ApiError, Elasticsearch
from pydantic import BaseModel, Field, ValidationError

from intelligent_reimbursement_assitant.content_index.domain.config import ElasticsearchSettings
from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    Chunk,
    IndexFilterMetadata,
    IndexRecord,
    IndexRecordBatch,
)
from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError


def content_index_definition() -> dict:
    def chinese_text() -> dict:
        return {
            "type": "text",
            "analyzer": "ik_max_word",
            "search_analyzer": "ik_smart",
            "fields": {
                "smartcn": {"type": "text", "analyzer": "smartcn", "search_analyzer": "smartcn"}
            },
        }

    title = chinese_text()
    title["fields"]["keyword"] = {"type": "keyword"}
    return {
        "settings": {"number_of_shards": 1, "number_of_replicas": 0},
        "mappings": {
            "dynamic": "strict",
            "properties": {
                "chunk": {
                    "properties": {
                        "chunk_id": {"type": "keyword"},
                        "document_id": {"type": "keyword"},
                        "source_hash": {"type": "keyword"},
                        "generation_id": {"type": "keyword"},
                        "ordinal": {"type": "integer"},
                        "body_text": {"type": "text", "index": False},
                        "retrieval_text": chinese_text(),
                        "locators": {"type": "object", "enabled": False},
                    }
                },
                "dense_vector": {
                    "type": "dense_vector",
                    "element_type": "float",
                    "dims": 1024,
                    "index": True,
                    "similarity": "cosine",
                    "index_options": {"type": "hnsw"},
                },
                "filter_fields": {
                    "properties": {
                        "kb_id": {"type": "keyword"},
                        "version_status": {"type": "keyword"},
                        "effective_from": {"type": "date", "format": "strict_date"},
                        "effective_to": {"type": "date", "format": "strict_date"},
                        "title": title,
                    }
                },
            },
        },
    }


def serialize_index_record(record: IndexRecord) -> dict:
    return record.model_dump(mode="json")


class _ItemError(BaseModel):
    type: str


class _GetItem(BaseModel):
    id: str = Field(alias="_id")
    found: bool | None = Field(default=None, strict=True)
    error: _ItemError | None = None


class _MultiGet(BaseModel):
    docs: list[_GetItem]


class _BulkItem(BaseModel):
    id: str = Field(alias="_id")
    status: int = Field(strict=True)
    error: _ItemError | None = None


class _BulkResult(BaseModel):
    items: list[dict[str, _BulkItem]]


class _ShardFailure(BaseModel):
    reason: _ItemError


class _Shards(BaseModel):
    failed: int = Field(ge=0, strict=True)
    failures: list[_ShardFailure] = Field(default_factory=list)


class _RefreshResult(BaseModel):
    shards: _Shards = Field(alias="_shards")


class _TotalHits(BaseModel):
    value: int = Field(ge=0, strict=True)
    relation: Literal["eq"]


class _Hits(BaseModel):
    total: _TotalHits


class _SearchResult(_RefreshResult):
    timed_out: bool = Field(strict=True)
    hits: _Hits


class _ChunkSource(BaseModel):
    chunk: Chunk


class _ChunkHit(BaseModel):
    id: str = Field(alias="_id")
    source: _ChunkSource = Field(alias="_source")


class _ChunkHits(BaseModel):
    hits: list[_ChunkHit]


class _ChunkSearchResult(_RefreshResult):
    timed_out: bool = Field(strict=True)
    hits: _ChunkHits


class _UpdateFailure(BaseModel):
    status: int = Field(strict=True)
    cause: _ItemError | None = None


class _UpdateResult(BaseModel):
    timed_out: bool = Field(strict=True)
    total: int = Field(ge=0, strict=True)
    updated: int = Field(ge=0, strict=True)
    noops: int = Field(ge=0, strict=True)
    version_conflicts: int = Field(ge=0, strict=True)
    failures: list[_UpdateFailure]


_TEMPORARY_STATUSES = {429, 502, 503, 504}
_TEMPORARY_ERRORS = {
    "unavailable_shards_exception",
    "es_rejected_execution_exception",
    "node_not_connected_exception",
    "master_not_discovered_exception",
}


@contextmanager
def _operation_errors(code: ErrorCode) -> Iterator[None]:
    """Normalize only the SDK/response boundary of a safe index operation."""
    try:
        yield
    except (ConnectionError, ConnectionTimeout) as exc:
        raise ExecutionError(
            code, "LOCAL_RETRYABLE", "Content index is temporarily unavailable"
        ) from exc
    except ApiError as exc:
        temporary = exc.status_code in _TEMPORARY_STATUSES or (
            code == ErrorCode.INDEX_METADATA_SYNC_ERROR and exc.status_code == 409
        )
        category = "LOCAL_RETRYABLE" if temporary else "PERMANENT"
        raise ExecutionError(
            code, category, f"Content index operation failed (HTTP {exc.status_code})"
        ) from exc
    except (ValueError, SerializationError) as exc:
        raise ExecutionError(code, "PERMANENT", "Content index response is invalid") from exc


def _check_shards(shards: _Shards, code: ErrorCode = ErrorCode.INDEX_VISIBILITY_ERROR) -> None:
    if shards.failed:
        temporary = bool(shards.failures) and all(
            failure.reason.type in _TEMPORARY_ERRORS for failure in shards.failures
        )
        raise ExecutionError(
            code,
            "LOCAL_RETRYABLE" if temporary else "PERMANENT",
            "Content index returned shard failures",
        )


class ElasticsearchContentIndexAdapter:
    def __init__(self, settings: ElasticsearchSettings):
        self.index_name = settings.index_name
        self.client = Elasticsearch(
            str(settings.url),
            api_key=settings.api_key.get_secret_value() if settings.api_key is not None else None,
            ca_certs=(
                str(settings.ca_certs)
                if settings.ca_certs is not None
                else cast(DefaultType, DEFAULT)
            ),
            request_timeout=settings.request_timeout_seconds,
            max_retries=0,
            retry_on_timeout=False,
        )

    def close(self) -> None:
        self.client.close()

    async def _run_read[T](self, operation: Callable[[], T]) -> T:
        task = asyncio.create_task(asyncio.to_thread(operation))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    def _search_chunks(self, generation_ids: list[UUID], **parameters) -> list[Chunk]:
        with _operation_errors(ErrorCode.INDEX_READ_ERROR):
            response = self.client.search(
                index=self.index_name,
                source_includes=["chunk"],
                allow_partial_search_results=False,
                **parameters,
            )
            result = _ChunkSearchResult.model_validate(response.body)
            _check_shards(result.shards, ErrorCode.INDEX_READ_ERROR)
            if result.timed_out:
                raise ExecutionError(
                    ErrorCode.INDEX_READ_ERROR, "LOCAL_RETRYABLE", "Content index read timed out"
                )
            allowed = set(generation_ids)
            chunks = []
            for hit in result.hits.hits:
                chunk = hit.source.chunk
                if (
                    hit.id != chunk.chunk_id
                    or chunk.generation_id not in allowed
                    or chunk.ordinal < 0
                    or any(
                        locator.document_id != chunk.document_id
                        or locator.source_hash != chunk.source_hash
                        for locator in chunk.locators
                    )
                ):
                    raise ValueError("Index response identity is outside the requested scope")
                chunks.append(chunk)
            return chunks

    async def keyword_search(
        self, text: str, document_ids: list[UUID], generation_ids: list[UUID], limit: int
    ) -> list[Chunk]:
        return await self._run_read(
            lambda: self._search_chunks(
                generation_ids,
                query={
                    "bool": {
                        "must": [{"match": {"chunk.retrieval_text": text}}],
                        "filter": [
                            {"terms": {"chunk.generation_id": [str(g) for g in generation_ids]}},
                            (
                                {"term": {"chunk.document_id": str(document_ids[0])}}
                                if len(document_ids) == 1
                                else {
                                    "terms": {
                                        "chunk.document_id": [str(key) for key in document_ids]
                                    }
                                }
                            ),
                        ],
                    }
                },
                size=limit,
                sort=[{"_score": "desc"}, {"chunk.chunk_id": "asc"}],
            )
        )

    async def semantic_search(
        self, query_vector: list[float], generation_ids: list[UUID], limit: int
    ) -> list[Chunk]:
        return await self._run_read(
            lambda: self._search_chunks(
                generation_ids,
                knn={
                    "field": "dense_vector",
                    "query_vector": query_vector,
                    "k": limit,
                    "num_candidates": min(10000, max(100, 10 * limit)),
                    "filter": {"terms": {"chunk.generation_id": [str(g) for g in generation_ids]}},
                },
                size=limit,
                sort=[{"_score": "desc"}, {"chunk.chunk_id": "asc"}],
            )
        )

    async def chunk_read(
        self, chunk_ids: list[str], generation_ids: list[UUID], neighbor_radius: int
    ) -> list[Chunk]:
        return await self._run_read(
            lambda: self._read_windows(chunk_ids, generation_ids, neighbor_radius)
        )

    def _read_windows(
        self, chunk_ids: list[str], generation_ids: list[UUID], radius: int
    ) -> list[Chunk]:
        seeds = self._search_chunks(
            generation_ids,
            query={
                "bool": {
                    "filter": [
                        {"terms": {"chunk.chunk_id": chunk_ids}},
                        {"terms": {"chunk.generation_id": [str(g) for g in generation_ids]}},
                    ]
                }
            },
            size=len(chunk_ids),
        )
        if {chunk.chunk_id for chunk in seeds} != set(chunk_ids):
            raise ExecutionError(
                ErrorCode.INDEX_READ_ERROR, "PERMANENT", "Known read seed is missing"
            )
        if radius == 0:
            return sorted(seeds, key=lambda c: (c.generation_id, c.ordinal, c.chunk_id))
        windows: dict[UUID, list[tuple[int, int]]] = {}
        for seed in seeds:
            windows.setdefault(seed.generation_id, []).append(
                (max(0, seed.ordinal - radius), seed.ordinal + radius)
            )
        selected = {chunk.chunk_id: chunk for chunk in seeds}
        for generation, ranges in sorted(windows.items()):
            merged: list[tuple[int, int]] = []
            for lower, upper in sorted(ranges):
                if merged and lower <= merged[-1][1] + 1:
                    merged[-1] = (merged[-1][0], max(upper, merged[-1][1]))
                else:
                    merged.append((lower, upper))
            for lower, upper in merged:
                # Keep each ordinal window within ES's default result window.
                for start in range(lower, upper + 1, 10000):
                    end = min(upper, start + 9999)
                    chunks = self._search_chunks(
                        [generation],
                        query={
                            "bool": {
                                "filter": [
                                    {"term": {"chunk.generation_id": str(generation)}},
                                    {"range": {"chunk.ordinal": {"gte": start, "lte": end}}},
                                ]
                            }
                        },
                        size=end - start + 1,
                        sort=[{"chunk.generation_id": "asc"}, {"chunk.ordinal": "asc"}],
                    )
                    if any(not start <= chunk.ordinal <= end for chunk in chunks):
                        raise ExecutionError(
                            ErrorCode.INDEX_READ_ERROR, "PERMANENT", "Read escaped ordinal window"
                        )
                    selected.update((chunk.chunk_id, chunk) for chunk in chunks)
        return sorted(selected.values(), key=lambda c: (c.generation_id, c.ordinal, c.chunk_id))

    def sync_filter_metadata(self, generation_id: UUID, metadata: IndexFilterMetadata) -> None:
        with _operation_errors(ErrorCode.INDEX_METADATA_SYNC_ERROR):
            response = self.client.update_by_query(
                index=self.index_name,
                query={"term": {"chunk.generation_id": str(generation_id)}},
                script={
                    "source": "ctx._source.filter_fields = params.metadata",
                    "params": {"metadata": metadata.model_dump(mode="json")},
                    "lang": "painless",
                },
                conflicts="abort",
                wait_for_completion=True,
                refresh=True,
            )
            result = _UpdateResult.model_validate(response.body)
            permanent = any(
                failure.status not in _TEMPORARY_STATUSES | {409}
                and (failure.cause is None or failure.cause.type not in _TEMPORARY_ERRORS)
                for failure in result.failures
            )
            if permanent or result.updated + result.noops > result.total:
                raise ExecutionError(
                    ErrorCode.INDEX_METADATA_SYNC_ERROR,
                    "PERMANENT",
                    "Content index metadata response failed",
                )
            if (
                result.timed_out
                or result.failures
                or result.version_conflicts
                or result.updated + result.noops != result.total
            ):
                raise ExecutionError(
                    ErrorCode.INDEX_METADATA_SYNC_ERROR,
                    "LOCAL_RETRYABLE",
                    "Content index metadata synchronization is incomplete",
                )

    def create_index(self) -> None:
        """Explicit deployment action; never called by an ingestion task."""
        self.client.indices.create(index=self.index_name, body=content_index_definition())

    def write_records(self, records: IndexRecordBatch) -> None:
        operations = []
        for record in records:
            operations.append({"create": {"_id": record.chunk.chunk_id}})
            operations.append(serialize_index_record(record))
        with _operation_errors(ErrorCode.INDEX_WRITE_ERROR):
            response = self.client.bulk(index=self.index_name, operations=operations, refresh=False)
            result = _BulkResult.model_validate(response.body)
            if len(result.items) != len(records):
                raise ValueError("Bulk response count does not match the request")
            temporary_failure = False
            permanent_failure = False
            for record, actions in zip(records, result.items, strict=False):
                if set(actions) != {"create"}:
                    raise ValueError("Bulk response action does not match the request")
                item = actions["create"]
                if item.id != record.chunk.chunk_id:
                    raise ValueError("Bulk response identity does not match the request")
                if item.status == 201 and item.error is None:
                    continue
                if (
                    item.status == 409
                    and item.error is not None
                    and item.error.type == "version_conflict_engine_exception"
                ):
                    continue
                if item.status not in {201, 409} and (
                    item.status in _TEMPORARY_STATUSES
                    or (item.error is not None and item.error.type in _TEMPORARY_ERRORS)
                ):
                    temporary_failure = True
                else:
                    permanent_failure = True
            # Interpret every item before deciding whether replay can resolve the batch.
            if permanent_failure or temporary_failure:
                raise ExecutionError(
                    ErrorCode.INDEX_WRITE_ERROR,
                    "PERMANENT" if permanent_failure else "LOCAL_RETRYABLE",
                    "Content index returned failed create items",
                )

    def confirm_queryable(self, generation_id: UUID, expected_count: int) -> None:
        with _operation_errors(ErrorCode.INDEX_VISIBILITY_ERROR):
            refreshed = self.client.indices.refresh(index=self.index_name)
            _check_shards(_RefreshResult.model_validate(refreshed.body).shards)
            response = self.client.search(
                index=self.index_name,
                query={"term": {"chunk.generation_id": str(generation_id)}},
                size=0,
                track_total_hits=True,
                allow_partial_search_results=False,
            )
            result = _SearchResult.model_validate(response.body)
            _check_shards(result.shards)
            if result.hits.total.value > expected_count:
                raise ExecutionError(
                    ErrorCode.INDEX_VISIBILITY_ERROR,
                    "PERMANENT",
                    "Content index generation exceeds the expected count",
                )
            if result.timed_out or result.hits.total.value < expected_count:
                raise ExecutionError(
                    ErrorCode.INDEX_VISIBILITY_ERROR,
                    "LOCAL_RETRYABLE",
                    "Content index generation is not fully queryable yet",
                )

    def existing_record_ids(self, chunk_ids: list[str]) -> set[str]:
        try:
            response = self.client.mget(
                index=self.index_name, ids=chunk_ids, source=False, realtime=True
            )
        except (ConnectionError, ConnectionTimeout) as exc:
            raise ExecutionError(
                ErrorCode.INDEX_READ_ERROR,
                "LOCAL_RETRYABLE",
                "Content index is temporarily unavailable",
            ) from exc
        except ApiError as exc:
            category = "LOCAL_RETRYABLE" if exc.status_code in _TEMPORARY_STATUSES else "PERMANENT"
            raise ExecutionError(
                ErrorCode.INDEX_READ_ERROR,
                category,
                f"Content index read failed (HTTP {exc.status_code})",
            ) from exc
        try:
            result = _MultiGet.model_validate(response.body)
            if len(result.docs) != len(chunk_ids) or {doc.id for doc in result.docs} != set(
                chunk_ids
            ):
                raise ValueError("Index response identities do not match the request")
            for doc in result.docs:
                if doc.error is not None:
                    # Per-item failures cannot be interpreted as missing records.
                    temporary = doc.error.type in _TEMPORARY_ERRORS
                    raise ExecutionError(
                        ErrorCode.INDEX_READ_ERROR,
                        "LOCAL_RETRYABLE" if temporary else "PERMANENT",
                        "Content index returned an item error",
                    )
                if doc.found is None:
                    raise ValueError("Index response is missing found/error")
            return {doc.id for doc in result.docs if doc.found}
        except (ValidationError, ValueError) as exc:
            raise ExecutionError(
                ErrorCode.INDEX_READ_ERROR, "PERMANENT", "Content index response is invalid"
            ) from exc
