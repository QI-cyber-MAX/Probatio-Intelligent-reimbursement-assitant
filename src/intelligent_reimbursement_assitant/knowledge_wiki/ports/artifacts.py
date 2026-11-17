from typing import Protocol
from uuid import UUID

from intelligent_reimbursement_assitant.contracts.models import ArtifactRef
from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    Chunk,
    ChunkBatch,
    SnapshotEntry,
)
from intelligent_reimbursement_assitant.document_ingestion_service.ports.repository import (
    PublishedSourceArtifacts,
)
from intelligent_reimbursement_assitant.knowledge_wiki.domain.models import (
    WikiChunkRef,
    WikiPackage,
)
from intelligent_reimbursement_assitant.parser.domain.models import ParsedDocument


class WikiBuildReaderPort(Protocol):
    def read_package(self, ref: ArtifactRef, kb_id: UUID) -> WikiPackage: ...
    def read_chunks(
        self, entry: SnapshotEntry, artifacts: PublishedSourceArtifacts
    ) -> ChunkBatch: ...
    def read_parsed(
        self, entry: SnapshotEntry, artifacts: PublishedSourceArtifacts
    ) -> ParsedDocument: ...
    def read_source_chunks(
        self,
        refs: list[WikiChunkRef],
        entries: list[SnapshotEntry],
        artifacts: dict[UUID, PublishedSourceArtifacts],
        neighbor_radius: int,
    ) -> list[Chunk]: ...
