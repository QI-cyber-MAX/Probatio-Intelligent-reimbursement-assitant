from pydantic import BaseModel, ConfigDict, Field

from intelligent_reimbursement_assitant.outbox_publisher.domain.config import PublisherSettings


class MaintenanceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    dispatcher_interval: float = Field(default=5, gt=0)
    dispatcher_batch_size: int = Field(default=20, gt=0, strict=True)
    publisher_interval: float = Field(default=1, gt=0)
    queued_stale_seconds: float = Field(default=300, gt=0)
    wakeup_interval_seconds: float = Field(default=300, gt=0)
    redis_connect_timeout: float = Field(default=2, gt=0)
    redis_command_timeout: float = Field(default=3, gt=0)
    publisher: PublisherSettings = PublisherSettings()
