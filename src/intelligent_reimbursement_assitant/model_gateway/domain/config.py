from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    SecretStr,
    field_validator,
    model_validator,
)


class ModelSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: HttpUrl
    api_key: SecretStr = Field(min_length=1)
    model_id: Literal["deepseek-v4-pro", "deepseek-flash"] = "deepseek-v4-pro"
    context_window_tokens: int = Field(default=1048576, strict=True, gt=0, le=1048576)
    max_output_tokens: int = Field(default=65536, strict=True, gt=0, le=393216)
    request_timeout_seconds: float = Field(default=60, gt=0, allow_inf_nan=False)

    @field_validator("api_key")
    @classmethod
    def nonblank_key(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("A nonblank key is required")
        return value

    @model_validator(mode="after")
    def output_capacity(self) -> ModelSettings:
        if self.max_output_tokens > self.context_window_tokens:
            raise ValueError("Output exceeds context capacity")
        return self
