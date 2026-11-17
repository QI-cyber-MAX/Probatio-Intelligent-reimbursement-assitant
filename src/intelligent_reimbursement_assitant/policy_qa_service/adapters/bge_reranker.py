"""PPIO rerank protocol; account native usage before validating candidate rankings."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from intelligent_reimbursement_assitant.contracts.models import ModelUsage
from intelligent_reimbursement_assitant.errors.codes import ErrorCode as Code
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.policy_qa_service.domain.config import RerankerSettings


class _Usage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int | None = Field(default=None, ge=0)


class _Envelope(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: str
    usage: _Usage


class _Result(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    index: int = Field(ge=0)
    relevance_score: float = Field(allow_inf_nan=False)


class _Ranking(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    results: list[_Result]


class PPIOBgeReranker:
    def __init__(
        self,
        settings: RerankerSettings,
        api_key: str,
        count_pair_tokens: Callable[[str, str], int],
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        io_pool: asyncio.Semaphore | None = None,
    ):
        self.settings = settings
        self.count_pair_tokens = count_pair_tokens
        self.client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=settings.request_timeout_seconds,
            transport=transport or httpx.AsyncHTTPTransport(retries=0),
            follow_redirects=False,
        )
        self.io_pool = io_pool or asyncio.Semaphore(4)
        self._request_lock = asyncio.Lock()
        self._next_request = 0.0

    async def aclose(self) -> None:
        await self.client.aclose()

    async def rerank(
        self, question: str, texts: list[str], *, check_execution: Callable[[], Awaitable[bool]]
    ) -> tuple[dict[str, JsonValue], ModelUsage]:
        if any(
            self.count_pair_tokens(question, text) > self.settings.max_pair_tokens for text in texts
        ):
            raise ExecutionError(
                Code.RERANK_INPUT_TOO_LARGE, "PERMANENT", "Reranker pair exceeds capacity"
            )
        async with self.io_pool:
            task = None
            try:
                async with self._request_lock:
                    await asyncio.sleep(max(0, self._next_request - time.monotonic()))
                    if not await check_execution():
                        raise asyncio.CancelledError("Execution no longer permitted")
                    started = asyncio.Event()

                    def on_send():
                        self._next_request = (
                            time.monotonic() + 60 / self.settings.requests_per_minute
                        )
                        started.set()

                    task = asyncio.create_task(self._request(question, texts, on_send))
                    await started.wait()
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task is not None:
                    await task
                raise

    async def _request(
        self, question: str, texts: list[str], on_send: Callable[[], None]
    ) -> tuple[dict[str, JsonValue], ModelUsage]:
        started = time.monotonic()
        on_send()
        try:
            response = await self.client.post(
                str(self.settings.endpoint),
                json={
                    "model": self.settings.model_id,
                    "query": question,
                    "documents": texts,
                    "top_n": len(texts),
                },
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as error:
            raise ExecutionError(
                Code.RERANK_UNAVAILABLE, "LOCAL_RETRYABLE", "Reranker connection unavailable"
            ) from error
        except httpx.RequestError as error:
            raise ExecutionError(
                Code.RERANK_OUTCOME_UNKNOWN, "OUTCOME_UNKNOWN", "Reranker request outcome unknown"
            ) from error
        if response.status_code == 429:
            raise ExecutionError(
                Code.RERANK_UNAVAILABLE, "LOCAL_RETRYABLE", "Reranker request rate exceeded"
            )
        if response.status_code in {400, 401, 402, 404, 422}:
            raise ExecutionError(
                Code.RERANK_CONFIGURATION_ERROR, "PERMANENT", "Reranker rejected the request"
            )
        if response.status_code != 200:
            raise ExecutionError(
                Code.RERANK_OUTCOME_UNKNOWN, "OUTCOME_UNKNOWN", "Reranker request outcome unknown"
            )
        try:
            raw = response.json()
            envelope = _Envelope.model_validate(raw)
        except ValueError as error:
            raise ExecutionError(
                Code.RERANK_OUTCOME_UNKNOWN, "OUTCOME_UNKNOWN", "Reranker usage outcome unknown"
            ) from error
        return cast(dict[str, JsonValue], raw), ModelUsage(
            model_id=self.settings.model_id,
            input_tokens=envelope.usage.prompt_tokens,
            output_tokens=envelope.usage.completion_tokens,
            elapsed_ms=int((time.monotonic() - started) * 1000),
            request_id=envelope.id,
        )

    def parse_ranking(self, response: dict[str, JsonValue], candidate_count: int) -> list[int]:
        try:
            ranking = _Ranking.model_validate(response)
            if len(ranking.results) != candidate_count or {
                result.index for result in ranking.results
            } != set(range(candidate_count)):
                raise ValueError("Reranker positions do not cover the candidates")
        except ValueError as error:
            raise ExecutionError(
                Code.RERANK_INVALID_RESPONSE, "PERMANENT", "Reranker ranking is invalid"
            ) from error
        return [
            result.index
            for result in sorted(
                ranking.results, key=lambda result: (-result.relevance_score, result.index)
            )
        ]
