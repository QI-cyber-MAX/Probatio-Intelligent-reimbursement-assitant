from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PublisherSettings(BaseModel):
    """Server-only controls; no configuration is added to messages or tasks."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    batch_size: int = Field(default=10, gt=0, strict=True)
    attempts_per_second: float = Field(default=1, gt=0)
    high_watermark: int = Field(default=6, gt=0, strict=True)
    low_watermark: int = Field(default=2, ge=0, strict=True)
    sample_interval: float = Field(default=1, gt=0)
    backoff_initial: float = Field(default=1, gt=0)
    backoff_max: float = Field(default=30, gt=0)

    @model_validator(mode="after")
    def ordered_limits(self) -> Self:
        if self.low_watermark >= self.high_watermark:
            raise ValueError("low_watermark must be less than high_watermark")
        if self.backoff_initial > self.backoff_max:
            raise ValueError("backoff_initial must not exceed backoff_max")
        return self
