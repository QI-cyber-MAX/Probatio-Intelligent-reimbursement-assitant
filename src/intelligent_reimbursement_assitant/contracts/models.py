"""Shared artifact, source identity, location, and model usage values."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    sha256: str
    media_type: str
    size_bytes: int


class SourceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: UUID
    source_hash: str


class SourceLocator(SourceReference):
    block_ids: list[str]
    page_start: int | None = None
    page_end: int | None = None
    section_path: list[str]
    sheet_name: str | None = None
    cell_range: str | None = None
    paragraph_index: int | None = None


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    model_id: str
    input_tokens: int | None
    output_tokens: int | None
    elapsed_ms: int
    request_id: str | None
