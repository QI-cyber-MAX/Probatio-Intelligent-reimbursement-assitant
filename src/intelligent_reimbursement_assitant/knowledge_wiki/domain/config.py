from pydantic import BaseModel, ConfigDict, Field

from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionSettings,
)
from intelligent_reimbursement_assitant.model_gateway.domain.config import ModelSettings
from intelligent_reimbursement_assitant.worker_runtime.domain.config import WorkerSettings


class WikiSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    model: ModelSettings
    execution: ExecutionSettings
    worker: WorkerSettings = Field(default_factory=WorkerSettings)
    discovery_interval_seconds: float = Field(default=259200, gt=0, allow_inf_nan=False)
    tool_concurrency: int = Field(default=4, strict=True, gt=0)
    model_concurrency: int = Field(default=4, gt=0, strict=True)
    structure_repair_limit: int = Field(default=3, ge=0, strict=True)
