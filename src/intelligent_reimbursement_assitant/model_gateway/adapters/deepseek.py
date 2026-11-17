"""DeepSeek native JSON Output, without SDK or HTTP retry layers."""

from dataclasses import dataclass
from time import monotonic
from typing import Annotated

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError

from intelligent_reimbursement_assitant.contracts.models import ModelUsage
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.model_gateway.domain.config import ModelSettings

TokenCount = Annotated[int, Field(strict=True, ge=0)]


class _Usage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    prompt_tokens: TokenCount | None = None
    completion_tokens: TokenCount | None = None


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    model: str
    id: str | None = None
    usage: _Usage | None = None


@dataclass(frozen=True)
class ModelErrorCodes:
    configuration: str
    unavailable: str
    outcome_unknown: str


class DeepSeekModelGateway:
    def __init__(
        self, client: httpx.AsyncClient, settings: ModelSettings, error_codes: ModelErrorCodes
    ):
        self.error_codes = error_codes
        self.client = client
        self.settings = settings

    async def generate_structured(
        self, messages: list[dict[str, JsonValue]], response_schema: dict[str, JsonValue]
    ) -> tuple[dict[str, JsonValue], ModelUsage]:
        return await self._post(
            {
                "messages": messages,
                "response_format": {"type": "json_object"},
            }
        )

    async def decide_tools(
        self, messages: list[dict[str, JsonValue]], tools: list[dict[str, JsonValue]]
    ) -> tuple[dict[str, JsonValue], ModelUsage]:
        return await self._post({"messages": messages, "tools": tools, "tool_choice": "auto"})

    async def _post(self, body: dict[str, object]) -> tuple[dict[str, JsonValue], ModelUsage]:
        started = monotonic()
        try:
            response = await self.client.post(
                str(self.settings.endpoint),
                headers={"Authorization": f"Bearer {self.settings.api_key.get_secret_value()}"},
                json={
                    "model": self.settings.model_id,
                    "thinking": {"type": "enabled"},
                    "stream": False,
                    "max_tokens": self.settings.max_output_tokens,
                    **body,
                },
                timeout=self.settings.request_timeout_seconds,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as error:
            raise ExecutionError(
                self.error_codes.unavailable, "LOCAL_RETRYABLE", "Connection not established"
            ) from error
        except httpx.HTTPError as error:
            raise ExecutionError(
                self.error_codes.outcome_unknown, "OUTCOME_UNKNOWN", "Request outcome unknown"
            ) from error
        if response.status_code == 429:
            raise ExecutionError(
                self.error_codes.unavailable, "LOCAL_RETRYABLE", "Request rate limited"
            )
        if response.status_code in (400, 401, 402, 422):
            raise ExecutionError(
                self.error_codes.configuration,
                "PERMANENT",
                "Model request configuration rejected",
            )
        if not response.is_success:
            raise ExecutionError(
                self.error_codes.outcome_unknown, "OUTCOME_UNKNOWN", "Model request outcome unknown"
            )
        try:
            raw = response.json()
            envelope = _Response.model_validate(raw)
        except (ValueError, ValidationError) as error:
            raise ExecutionError(
                self.error_codes.outcome_unknown,
                "OUTCOME_UNKNOWN",
                "Model usage cannot be confirmed",
            ) from error
        usage = envelope.usage
        return raw, ModelUsage(
            model_id=envelope.model,
            input_tokens=usage.prompt_tokens if usage else None,
            output_tokens=usage.completion_tokens if usage else None,
            elapsed_ms=int((monotonic() - started) * 1000),
            request_id=envelope.id,
        )
