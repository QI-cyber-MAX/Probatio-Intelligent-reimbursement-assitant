from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, model_validator

from intelligent_reimbursement_assitant.execution_state_service.domain.models import ExecutionKind


class WorkerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    concurrency: int = Field(default=1, gt=0, strict=True)
    lease_seconds: float = Field(default=60, gt=0)
    renew_seconds: float = Field(default=15, gt=0)
    read_block_ms: int = Field(default=1000, gt=0, le=1000, strict=True)
    reclaim_seconds: float = Field(default=10, gt=0)
    pending_idle_ms: int = Field(default=120000, gt=0, strict=True)
    grace_seconds: float = Field(default=30, gt=0)
    redis_connect_timeout: float = Field(default=2, gt=0)
    redis_command_timeout: float = Field(default=3, gt=0)

    @model_validator(mode="after")
    def relationships(self):
        if self.renew_seconds >= self.lease_seconds:
            raise ValueError("renew_seconds must be shorter than lease_seconds")
        if self.redis_command_timeout <= self.read_block_ms / 1000:
            raise ValueError("Redis command timeout must exceed blocking read")
        return self


@dataclass(frozen=True)
class QueueBinding:
    kind: ExecutionKind
    stream: str
    group: str
