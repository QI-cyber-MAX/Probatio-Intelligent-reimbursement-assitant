from typing import Protocol
from uuid import UUID

from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    Chunk,
    IndexFilterMetadata,
    IndexRecordBatch,
)


class ContentIndexPort(Protocol):
    async def keyword_search(
        self, text: str, document_ids: list[UUID], generation_ids: list[UUID], limit: int
    ) -> list[Chunk]: ...

    async def semantic_search(
        self, query_vector: list[float], generation_ids: list[UUID], limit: int
    ) -> list[Chunk]: ...

    async def chunk_read(
        self, chunk_ids: list[str], generation_ids: list[UUID], neighbor_radius: int
    ) -> list[Chunk]: ...

    def existing_record_ids(self, chunk_ids: list[str]) -> set[str]: ...

    def write_records(self, records: IndexRecordBatch) -> None: ...

    def confirm_queryable(self, generation_id: UUID, expected_count: int) -> None: ...

    def sync_filter_metadata(self, generation_id: UUID, metadata: IndexFilterMetadata) -> None: ...
