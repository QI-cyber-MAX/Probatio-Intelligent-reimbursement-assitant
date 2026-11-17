from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr


class EmbeddingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: HttpUrl
    api_key: SecretStr | None = Field(default=None, min_length=1)
    model_id: Literal["baai/bge-m3"] = "baai/bge-m3"
    batch_size: int = Field(default=16, gt=0, le=2048, strict=True)
    request_timeout_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    # PPIO BGE-M3 T2: 1,000 RPM / 50,000,000 TPM. Requests are serialized.
    requests_per_minute: int = Field(default=1000, gt=0, le=1000, strict=True)
