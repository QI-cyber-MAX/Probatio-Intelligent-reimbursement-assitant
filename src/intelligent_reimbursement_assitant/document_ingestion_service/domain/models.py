"""Document registration objects and policy metadata."""

import re
from datetime import date, datetime
from enum import StrEnum
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from intelligent_reimbursement_assitant.contracts.models import (
    ArtifactRef,
    SourceLocator,
    SourceReference,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    IngestionJobView,
)
from intelligent_reimbursement_assitant.scanner.domain.models import ScanReport


class KnowledgeBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kb_id: UUID
    name: str
    revision: int
    active_index_profile_id: str
    created_at: datetime


class VersionStatus(StrEnum):
    CURRENT = "CURRENT"
    HISTORICAL = "HISTORICAL"


class PolicyDateSource(StrEnum):
    USER = "USER"
    DOCUMENT = "DOCUMENT"


class PolicyDate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    value: str
    source: PolicyDateSource
    locators: list[SourceLocator] = Field(default_factory=list)

    @field_validator("value")
    @classmethod
    def calendar_value(cls, value: str) -> str:
        if not re.fullmatch(r"\d{4}-\d{2}(?:-\d{2})?", value):
            raise ValueError("expected YYYY-MM or YYYY-MM-DD")
        date.fromisoformat(value + "-01" if len(value) == 7 else value)
        return value

    @model_validator(mode="after")
    def date_authority(self) -> Self:
        if self.source == PolicyDateSource.USER and (len(self.value) != 10 or self.locators):
            raise ValueError("user dates require day precision and no source locators")
        return self


class DocumentRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kb_id: UUID
    issuer: str = Field(min_length=1)
    version_status: VersionStatus
    effective_from: date | None = None
    effective_to: date | None = None
    document_id: UUID | None = None
    title: str | None = Field(default=None, min_length=1)

    @field_validator("issuer", "title", mode="before")
    @classmethod
    def trim_identity(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @field_validator("effective_from", "effective_to", mode="before")
    @classmethod
    def iso_date(cls, value: object) -> object:
        if value is None or type(value) is date:
            return value
        if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
            return date.fromisoformat(value)
        raise ValueError("expected an ISO calendar date")

    @model_validator(mode="after")
    def choose_document(self) -> Self:
        if (self.document_id is None) == (self.title is None):
            raise ValueError("provide exactly one of document_id and title")
        if self.effective_from and self.effective_to and self.effective_to <= self.effective_from:
            raise ValueError("effective_to must be later than effective_from")
        return self


class SourceMetadataUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    new_job_id: UUID
    version_status: VersionStatus | None = None
    effective_from: date | None = None
    effective_to: date | None = None

    _iso_date = field_validator("effective_from", "effective_to", mode="before")(
        DocumentRegistration.iso_date.__func__
    )

    @model_validator(mode="after")
    def submitted_changes(self) -> Self:
        if not self.model_fields_set - {"new_job_id"}:
            raise ValueError("submit at least one metadata field")
        if "version_status" in self.model_fields_set and self.version_status is None:
            raise ValueError("version_status cannot be null")
        return self


class Document(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: UUID
    kb_id: UUID
    issuer: str
    title: str
    policy_identity: str | None = None


class Chunk(SourceReference):
    chunk_id: str
    generation_id: UUID
    ordinal: int
    body_text: str
    retrieval_text: str
    locators: list[SourceLocator]


class ChunkBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chunks: list[Chunk]


class IndexFilterMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kb_id: UUID
    version_status: VersionStatus
    effective_from: date | None
    effective_to: date | None
    title: str


class IndexRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    chunk: Chunk
    dense_vector: list[float]
    filter_fields: IndexFilterMetadata


type IndexRecordBatch = list[IndexRecord]


class PolicyMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer: str
    policy_code: str | None = None
    version_status: VersionStatus
    effective_from: PolicyDate | None = None
    effective_to: PolicyDate | None = None
    source_locators: list[SourceLocator] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def read_persisted_metadata(cls, value: object) -> object:
        legacy_fields = {
            "issuer",
            "policy_code",
            "effective_from",
            "effective_to",
            "supersedes_sources",
            "source_locators",
        }
        if isinstance(value, dict) and set(value) == legacy_fields:
            legacy = dict(value)
            legacy.pop("supersedes_sources")
            legacy["version_status"] = VersionStatus.CURRENT
            for name in ("effective_from", "effective_to"):
                old = legacy.get(name)
                if old is not None:
                    legacy[name] = {"value": str(old), "source": "USER", "locators": []}
            return legacy
        return value


class DocumentSourceView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_hash: str
    uploaded_at: datetime
    policy_metadata: PolicyMetadata
    serving_generation_id: UUID | None
    latest_job: IngestionJobView


class DocumentView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: UUID
    kb_id: UUID
    issuer: str
    title: str
    versions: list[DocumentSourceView]


class SnapshotEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str | None = None

    source: SourceReference
    generation_id: UUID
    index_profile_id: str
    policy_metadata: PolicyMetadata


class KnowledgeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kb_id: UUID
    revision: int
    entries: list[SnapshotEntry]


class AcceptedUpload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_ref: ArtifactRef
    original_name: str
    detected_type: str
    source_hash: str
    scan_report: ScanReport


class AcceptanceStatus(StrEnum):
    ACCEPTED = "ACCEPTED"


class IngestionAccepted(SourceReference):
    job_id: UUID
    status: Literal[AcceptanceStatus.ACCEPTED] = AcceptanceStatus.ACCEPTED
    status_url: str
