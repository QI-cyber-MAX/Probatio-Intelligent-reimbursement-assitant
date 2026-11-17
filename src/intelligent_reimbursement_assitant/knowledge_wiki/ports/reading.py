from typing import Protocol
from uuid import UUID

from intelligent_reimbursement_assitant.contracts.models import ArtifactRef
from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    Chunk,
    SnapshotEntry,
)


class WikiReaderPort(Protocol):
    def project_page(
        self,
        page: dict,
        wiki_bindings: dict[str, str],
        chunk_bindings: dict[str, str],
        source_refs: dict[UUID, str],
    ) -> tuple[dict, dict[str, str], dict[str, str]]: ...
    def load_page(
        self,
        ref: ArtifactRef,
        kb_id: UUID,
        page_id: str,
        entries: list[SnapshotEntry],
    ) -> dict: ...
    def read_source_chunks(
        self,
        ref: ArtifactRef,
        kb_id: UUID,
        chunk_ids: list[str],
        entries: list[SnapshotEntry],
        neighbor_radius: int,
    ) -> list[Chunk]: ...
