"""The managed-file deserialization boundary shared by compilation and reading."""

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
from intelligent_reimbursement_assitant.errors.codes import ErrorCode as Code
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.knowledge_wiki.domain.models import (
    WikiChunkRef,
    WikiPackage,
)
from intelligent_reimbursement_assitant.parser.domain.models import ParsedDocument
from intelligent_reimbursement_assitant.scanner.ports.artifact_store import ArtifactStorePort


class WikiArtifactReader:
    def __init__(self, store: ArtifactStorePort):
        self.store = store
        self.packages: dict[tuple[ArtifactRef, UUID], WikiPackage] = {}
        self.batches: dict[tuple[UUID, ArtifactRef], ChunkBatch] = {}

    def read_package(self, ref: ArtifactRef, kb_id: UUID) -> WikiPackage:
        key = ref, kb_id
        if key not in self.packages:
            try:
                with self.store.open_artifact(ref) as source:
                    package = WikiPackage.model_validate_json(source.read())
                if package.snapshot.kb_id != kb_id:
                    raise ValueError("Wiki belongs to another knowledge base")
                self.packages[key] = package
            except (OSError, ValueError) as error:
                raise ExecutionError(
                    Code.WIKI_ARTIFACT_ERROR, "PERMANENT", "Wiki package unavailable"
                ) from error
        return self.packages[key]

    def read_chunks(self, entry: SnapshotEntry, artifacts: PublishedSourceArtifacts) -> ChunkBatch:
        key = entry.generation_id, artifacts.chunk_batch_ref
        if key not in self.batches:
            try:
                with self.store.open_artifact(artifacts.chunk_batch_ref) as source:
                    batch = ChunkBatch.model_validate_json(source.read())
                if not batch.chunks:
                    raise ValueError("Published chunk batch is empty")
                ids = set()
                ordinals = set()
                for chunk in batch.chunks:
                    if (chunk.document_id, chunk.source_hash, chunk.generation_id) != (
                        entry.source.document_id,
                        entry.source.source_hash,
                        entry.generation_id,
                    ):
                        raise ValueError("Chunk batch does not match fixed source and generation")
                    if chunk.chunk_id in ids or chunk.ordinal in ordinals:
                        raise ValueError("Duplicate chunk identity or ordinal")
                    if any(
                        (loc.document_id, loc.source_hash) != (chunk.document_id, chunk.source_hash)
                        for loc in chunk.locators
                    ):
                        raise ValueError("Chunk locator source mismatch")
                    ids.add(chunk.chunk_id)
                    ordinals.add(chunk.ordinal)
                self.batches[key] = batch
            except (OSError, ValueError) as error:
                raise ExecutionError(
                    Code.WIKI_SOURCE_UNAVAILABLE, "PERMANENT", "Published chunks unavailable"
                ) from error
        return self.batches[key]

    def read_parsed(
        self, entry: SnapshotEntry, artifacts: PublishedSourceArtifacts
    ) -> ParsedDocument:
        try:
            with self.store.open_artifact(artifacts.parsed_document_ref) as source:
                parsed = ParsedDocument.model_validate_json(source.read())
            if (parsed.document_id, parsed.source_hash) != (
                entry.source.document_id,
                entry.source.source_hash,
            ):
                raise ValueError("Parsed source mismatch")
            return parsed
        except (OSError, ValueError) as error:
            raise ExecutionError(
                Code.WIKI_SOURCE_UNAVAILABLE, "PERMANENT", "Published parse unavailable"
            ) from error

    def read_source_chunks(
        self,
        refs: list[WikiChunkRef],
        entries: list[SnapshotEntry],
        artifacts: dict[UUID, PublishedSourceArtifacts],
        neighbor_radius: int,
    ) -> list[Chunk]:
        allowed = {e.generation_id: e for e in entries}
        selected = {}
        for ref in refs:
            if ref.generation_id not in allowed:
                raise ExecutionError(
                    Code.WIKI_SOURCE_UNAVAILABLE, "PERMANENT", "Wiki source unavailable"
                )
            entry = allowed[ref.generation_id]
            batch = self.read_chunks(entry, artifacts[ref.generation_id])
            index = {c.chunk_id: c for c in batch.chunks}
            if ref.chunk_id not in index:
                raise ExecutionError(
                    Code.WIKI_SOURCE_UNAVAILABLE, "PERMANENT", "Referenced chunk missing"
                )
            seed = index[ref.chunk_id]
            for chunk in sorted(batch.chunks, key=lambda c: c.ordinal):
                if abs(chunk.ordinal - seed.ordinal) <= neighbor_radius:
                    selected[(chunk.generation_id, chunk.chunk_id)] = chunk
        return list(selected.values())
