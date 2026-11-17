from decimal import Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)

from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionSettings,
)
from intelligent_reimbursement_assitant.model_gateway.domain.config import ModelSettings
from intelligent_reimbursement_assitant.worker_runtime.domain.config import WorkerSettings


class RerankerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: HttpUrl = HttpUrl("https://api.ppio.com/openai/v1/rerank")
    model_id: Literal["baai/bge-reranker-v2-m3"] = "baai/bge-reranker-v2-m3"
    api_key_profile_id: str = Field(min_length=1, pattern=r"\S")
    tokenizer_dir: Path
    max_pair_tokens: int = Field(default=8192, strict=True, gt=0, le=8192)
    request_timeout_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)
    requests_per_minute: int = Field(default=30, strict=True, gt=0)

    @field_validator("tokenizer_dir")
    @classmethod
    def absolute_directory(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("An absolute path is required")
        return value


class QASettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    history_max_turns: int = Field(default=4, strict=True, ge=0)
    tokenizer_dir: Path
    execution: ExecutionSettings
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    finalization_reserve_input_tokens: int = Field(default=40000, strict=True, gt=0)
    model: ModelSettings | None = None
    business_timezone: str | None = None
    clarification_limit: int = Field(default=2, strict=True, ge=0)
    structure_repair_limit: int = Field(default=3, strict=True, ge=0)
    retrieval_round_limit: int = Field(default=3, strict=True, gt=0)
    tool_concurrency: int = Field(default=4, strict=True, gt=0)
    tool_step_limit: int = Field(default=12, strict=True, gt=0)
    candidate_preview_limit: int = Field(default=7, strict=True, gt=0)
    search_limit_per_profile: int = Field(default=10, strict=True, gt=0, le=10000)
    rerank_candidate_limit: int = Field(default=12, strict=True, gt=0)
    neighbor_radius: int = Field(default=1, strict=True, ge=0)
    read_chunk_limit: int = Field(default=30, strict=True, gt=0)
    reranker: RerankerSettings | None = None

    @model_validator(mode="after")
    def retrieval_capacity(self) -> QASettings:
        if self.finalization_reserve_input_tokens >= self.execution.budget_limit:
            raise ValueError("Finalization reserve must be smaller than the job budget")
        if self.candidate_preview_limit > self.rerank_candidate_limit:
            raise ValueError("Candidate preview exceeds rerank capacity")
        if self.reranker is not None and (
            self.rerank_candidate_limit
            * self.reranker.max_pair_tokens
            * self.reranker.requests_per_minute
            > 50_000_000
        ):
            raise ValueError("Reduce candidates or request rate to fit PPIO's 50M TPM")
        return self

    @field_validator("business_timezone")
    @classmethod
    def valid_timezone(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                ZoneInfo(value)
            except (ZoneInfoNotFoundError, ValueError) as error:
                raise ValueError("An IANA timezone is required") from error
        return value

    @field_validator("tokenizer_dir")
    @classmethod
    def absolute_directory(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("An absolute path is required")
        return value


def retrieval_budget_allowed(budget_used: Decimal, settings: QASettings) -> bool:
    return (
        budget_used < settings.execution.budget_limit - settings.finalization_reserve_input_tokens
    )
