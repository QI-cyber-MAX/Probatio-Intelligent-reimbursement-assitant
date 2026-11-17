from pydantic import BaseModel, ConfigDict, Field

from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionSettings,
)
from intelligent_reimbursement_assitant.scanner.domain.config import ScannerSettings
from intelligent_reimbursement_assitant.scanner.domain.models import UploadPolicy


class ApiSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    default_index_profile_id: str = Field(min_length=1, pattern=r"\S")

    policy: UploadPolicy = Field(default_factory=UploadPolicy)
    execution: ExecutionSettings
    scanner: ScannerSettings
    max_concurrent_uploads: int = Field(default=2, gt=0, strict=True)
    slot_wait_seconds: float = Field(default=2, gt=0)
    reception_timeout_seconds: float = Field(default=120, gt=0)
