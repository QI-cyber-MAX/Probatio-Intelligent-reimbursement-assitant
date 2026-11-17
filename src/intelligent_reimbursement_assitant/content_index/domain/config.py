from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, field_validator

from intelligent_reimbursement_assitant.embedding.domain.config import EmbeddingSettings


class ElasticsearchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: HttpUrl
    index_name: str = Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    api_key: SecretStr | None = Field(default=None, min_length=1)
    ca_certs: Path | None = None
    request_timeout_seconds: float = Field(default=30, gt=0, allow_inf_nan=False)

    @field_validator("ca_certs")
    @classmethod
    def absolute_paths(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("An absolute path is required")
        return value


class IndexProfileSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    elasticsearch: ElasticsearchSettings
    embedding: EmbeddingSettings
