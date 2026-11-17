from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from intelligent_reimbursement_assitant.model_gateway.domain.config import ModelSettings
from intelligent_reimbursement_assitant.parser.domain.config import ParserSettings
from intelligent_reimbursement_assitant.worker_runtime.domain.config import WorkerSettings


class ChunkingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    tokenizer_dir: Path
    strategy: Literal["rule", "model"] = "rule"
    model: ModelSettings | None = None
    model_tokenizer_dir: Path | None = None
    model_input_token_limit: int = Field(default=262144, gt=0, strict=True)
    target_tokens: int = Field(default=512, gt=0, strict=True)
    max_tokens: int = Field(default=1024, gt=0, strict=True)

    @model_validator(mode="after")
    def ordered_limits(self) -> Self:
        if self.target_tokens > self.max_tokens:
            raise ValueError("target_tokens must not exceed max_tokens")
        if self.strategy == "model" and (self.model is None or self.model_tokenizer_dir is None):
            raise ValueError("Model strategy requires model and model_tokenizer_dir")
        return self

    @field_validator("tokenizer_dir", "model_tokenizer_dir")
    @classmethod
    def absolute_paths(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("An absolute path is required")
        return value


class IngestionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    parser: ParserSettings
    chunking: ChunkingSettings

    @model_validator(mode="after")
    def chunk_capacity(self) -> Self:
        if self.chunking.max_tokens > 8192:
            raise ValueError("BGE-M3 supports at most 8192 tokens per input")
        return self
