"""PPIO's OpenAI-compatible dense embeddings protocol, with no SDK retries."""

import asyncio
import logging
import math
import time
from typing import Annotated, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from intelligent_reimbursement_assitant.contracts.models import ModelUsage
from intelligent_reimbursement_assitant.embedding.domain.config import EmbeddingSettings
from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError

logger = logging.getLogger(__name__)


class _Embedding(BaseModel):
    object: Literal["embedding"]
    index: int = Field(ge=0, strict=True)
    embedding: list[Annotated[float, Field(allow_inf_nan=False, strict=True)]] = Field(
        min_length=1024, max_length=1024
    )


class _Usage(BaseModel):
    prompt_tokens: int | None = Field(default=None, ge=0, strict=True)
    total_tokens: int | None = Field(default=None, ge=0, strict=True)


class _Response(BaseModel):
    model_config = ConfigDict(extra="ignore")

    object: Literal["list"]
    model: str
    data: list[_Embedding]
    usage: _Usage | None = None


class BgeM3ApiAdapter:
    def __init__(
        self, settings: EmbeddingSettings, *, transport: httpx.AsyncBaseTransport | None = None
    ):
        self.settings = settings
        headers = {}
        if settings.api_key is not None:
            headers["Authorization"] = f"Bearer {settings.api_key.get_secret_value()}"
        self.client = httpx.AsyncClient(
            headers=headers,
            timeout=settings.request_timeout_seconds,
            transport=transport or httpx.AsyncHTTPTransport(retries=0),
            follow_redirects=False,
        )
        self._request_lock = asyncio.Lock()
        self._next_request = 0.0

    async def aclose(self) -> None:
        await self.client.aclose()

    async def embed_query(self, text: str) -> tuple[list[float], ModelUsage]:
        vectors, usage = await self.embed_documents([text])
        return vectors[0], usage

    async def embed_documents(self, texts: list[str]) -> tuple[list[list[float]], ModelUsage]:
        async with self._request_lock:
            await asyncio.sleep(max(0, self._next_request - time.monotonic()))
            self._next_request = time.monotonic() + 60 / self.settings.requests_per_minute
            task = asyncio.create_task(self._request(texts))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                # Closing the local coroutine does not undo a billable remote request.
                # Resolve it (including unknown-outcome errors) before releasing resources.
                await task
                raise

    async def _request(self, texts: list[str]) -> tuple[list[list[float]], ModelUsage]:
        started = time.monotonic()
        request_id = None
        usage = None
        outcome = "failed"
        try:
            try:
                response = await self.client.post(
                    str(self.settings.endpoint),
                    json={
                        "model": self.settings.model_id,
                        "input": texts,
                        "encoding_format": "float",
                    },
                )
            except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
                raise ExecutionError(
                    ErrorCode.EMBEDDING_UNAVAILABLE,
                    "LOCAL_RETRYABLE",
                    "Embedding connection unavailable",
                ) from exc
            except httpx.TimeoutException as exc:
                raise ExecutionError(
                    ErrorCode.EMBEDDING_TIMEOUT,
                    "OUTCOME_UNKNOWN",
                    "PPIO request billing outcome is unknown",
                ) from exc
            except httpx.RequestError as exc:
                raise ExecutionError(
                    ErrorCode.EMBEDDING_UNAVAILABLE,
                    "OUTCOME_UNKNOWN",
                    "PPIO request outcome is unknown",
                ) from exc
            request_id = response.headers.get("x-request-id")
            if response.status_code == 429:
                raise ExecutionError(
                    ErrorCode.EMBEDDING_UNAVAILABLE, "LOCAL_RETRYABLE", "PPIO request rate exceeded"
                )
            if response.status_code == 408:
                raise ExecutionError(
                    ErrorCode.EMBEDDING_TIMEOUT,
                    "OUTCOME_UNKNOWN",
                    "PPIO request timed out at the server",
                )
            if response.status_code >= 500:
                # PPIO does not document per-request billing reconciliation or idempotency.
                raise ExecutionError(
                    ErrorCode.EMBEDDING_UNAVAILABLE,
                    "OUTCOME_UNKNOWN",
                    "PPIO returned a server error",
                )
            if response.status_code != 200:
                raise ExecutionError(
                    ErrorCode.EMBEDDING_CONFIGURATION_ERROR,
                    "PERMANENT",
                    f"PPIO rejected the embedding request (HTTP {response.status_code})",
                )
            try:
                result = _Response.model_validate_json(response.content)
                usage = result.usage
                if result.model != self.settings.model_id:
                    raise ValueError("Embedding response model does not match the bound profile")
                if len(result.data) != len(texts) or {item.index for item in result.data} != set(
                    range(len(texts))
                ):
                    raise ValueError("Embedding response indices do not match the inputs")
                vectors = [item.embedding for item in sorted(result.data, key=lambda x: x.index)]
                if any(
                    not math.isfinite(math.hypot(*vector)) or not any(vector) for vector in vectors
                ):
                    raise ValueError("Embedding response contains an invalid cosine vector")
            except (ValidationError, ValueError) as exc:
                # ValidationError can include input values: never expose its text or response body.
                raise ExecutionError(
                    ErrorCode.EMBEDDING_INVALID_RESPONSE,
                    "PERMANENT",
                    "PPIO embedding response is invalid",
                ) from exc
            outcome = "succeeded"
            return vectors, ModelUsage(
                model_id=result.model,
                input_tokens=usage.prompt_tokens if usage else None,
                output_tokens=None,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                request_id=request_id,
            )
        finally:
            logger.info(
                "embedding_request",
                extra={
                    "model_id": self.settings.model_id,
                    "outcome": outcome,
                    "input_tokens": usage.prompt_tokens if usage else None,
                    "elapsed_ms": int((time.monotonic() - started) * 1000),
                    "request_id": request_id,
                },
            )
