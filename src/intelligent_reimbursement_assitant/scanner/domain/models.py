"""Upload policies, quarantined files and scan results."""

from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DOC = "application/msword"
SUPPORTED_MEDIA_TYPES = (PDF, DOCX, XLSX, DOC)


class UploadPolicy(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    allowed_media_types: list[str] = Field(default_factory=lambda: list(SUPPORTED_MEDIA_TYPES))
    max_file_bytes: int = Field(default=50 * 1024**2, gt=0)
    max_request_bytes: int = Field(default=52 * 1024**2, gt=0)
    max_container_directory_bytes: int = Field(default=4 * 1024**2, gt=0)
    max_container_entries: int = Field(default=5000, gt=0)
    max_container_member_bytes: int = Field(default=50 * 1024**2, gt=0)
    max_container_expanded_bytes: int = Field(default=200 * 1024**2, gt=0)
    max_container_depth: int = Field(default=8, gt=0)
    scan_timeout_seconds: int = Field(default=30, gt=0)
    encrypted_file_policy: Literal["REJECT"] = "REJECT"
    macro_file_policy: Literal["REJECT"] = "REJECT"

    @model_validator(mode="after")
    def validate_limits(self) -> Self:
        media = self.allowed_media_types
        if not media or len(set(media)) != len(media):
            raise ValueError("allowed_media_types must be nonempty and unique")
        if not set(media).issubset(SUPPORTED_MEDIA_TYPES):
            raise ValueError("allowed_media_types contains an unsupported MIME")
        if self.max_request_bytes <= self.max_file_bytes:
            raise ValueError("max_request_bytes must exceed max_file_bytes")
        if self.max_container_expanded_bytes < self.max_container_member_bytes:
            raise ValueError("expanded byte limit must cover the member byte limit")
        return self


class QuarantinedUpload(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)

    temp_key: str
    original_name: str
    detected_type: str
    size_bytes: int = Field(gt=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ScanVerdict(StrEnum):
    CLEAN = "CLEAN"
    REJECTED = "REJECTED"
    INCOMPLETE = "INCOMPLETE"


class ScanReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    verdict: ScanVerdict
    reason_codes: list[str]
    scanner_version: str = Field(min_length=1)

    @model_validator(mode="after")
    def validate_report(self) -> Self:
        allowed = {
            "MALWARE_DETECTED",
            "ENCRYPTED_FILE",
            "MACROS_DETECTED",
            "SCAN_TIMEOUT",
            "SCANNER_UNAVAILABLE",
            "INVALID_SCANNER_RESPONSE",
            "SCANNER_ERROR",
            "SCAN_RESOURCE_LIMIT",
            "SCAN_STREAM_TOO_LARGE",
        }
        if not set(self.reason_codes).issubset(allowed):
            raise ValueError("unknown scan reason code")
        if (self.verdict == ScanVerdict.CLEAN) != (not self.reason_codes):
            raise ValueError("only CLEAN reports have an empty reason list")
        return self
