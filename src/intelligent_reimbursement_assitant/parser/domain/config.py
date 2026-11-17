from pathlib import Path
from typing import Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    field_validator,
    model_validator,
)


class MinerUSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    api_base_url: HttpUrl = HttpUrl("https://mineru.net")
    api_key: SecretStr = Field(min_length=1)
    request_timeout_seconds: float = Field(default=60, gt=0)
    poll_interval_seconds: float = Field(default=2, gt=0)
    max_result_bytes: int = Field(default=100 * 1024**2, gt=0, strict=True)
    max_uncompressed_bytes: int = Field(default=500 * 1024**2, gt=0, strict=True)

    @field_validator("api_key")
    @classmethod
    def nonblank_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("A nonblank key is required")
        return value


class ParserSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    artifacts_path: Path
    temporary_directory: Path
    pdf_backend: Literal["docling", "mineru"] = "docling"
    mineru: MinerUSettings | None = None
    libreoffice_path: Path | None = None
    timeout_seconds: float = Field(default=600, gt=0)
    terminate_seconds: float = Field(default=10, gt=0)
    threads: int = Field(default=2, gt=0, strict=True)

    @model_validator(mode="after")
    def backend_configuration(self) -> Self:
        if self.pdf_backend == "mineru" and self.mineru is None:
            raise ValueError("MinerU configuration is required")
        return self

    @field_validator("artifacts_path", "temporary_directory", "libreoffice_path")
    @classmethod
    def absolute_paths(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("An absolute path is required")
        return value
