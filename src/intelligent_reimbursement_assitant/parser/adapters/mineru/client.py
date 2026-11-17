"""Single-operation MinerU API and unauthenticated signed-object transfers."""

from typing import Literal
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, ValidationError

from intelligent_reimbursement_assitant.errors.codes import ErrorCode as Code
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.parser.domain.config import MinerUSettings


class UploadBatch(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    batch_id: str = Field(min_length=1)
    file_urls: list[HttpUrl] = Field(min_length=1, max_length=1)


class ExtractResult(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    state: Literal["waiting-file", "pending", "running", "converting", "done", "failed"]
    full_zip_url: HttpUrl | None = None
    err_msg: str = ""


class _BatchResult(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    batch_id: str
    extract_result: list[ExtractResult] = Field(min_length=1, max_length=1)


def _invalid() -> ExecutionError:
    return ExecutionError(Code.PARSE_RESULT_INVALID, "PERMANENT", "Invalid MinerU response")


class MinerUClient:
    def __init__(
        self, api: httpx.AsyncClient, objects: httpx.AsyncClient, settings: MinerUSettings
    ):
        self.api = api
        self.objects = objects
        self.settings = settings

    async def _api(self, method: str, path: str, *, body: dict | None = None) -> dict:
        try:
            response = await self.api.request(
                method,
                str(self.settings.api_base_url).rstrip("/") + path,
                headers={"Authorization": f"Bearer {self.settings.api_key.get_secret_value()}"},
                json=body,
                timeout=self.settings.request_timeout_seconds,
                follow_redirects=False,
            )
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as error:
            raise ExecutionError(
                Code.PARSER_UNAVAILABLE, "LOCAL_RETRYABLE", "Connection not established"
            ) from error
        except httpx.HTTPError as error:
            raise ExecutionError(
                Code.PARSER_UNAVAILABLE if method == "GET" else Code.EXECUTION_INTERNAL_ERROR,
                "LOCAL_RETRYABLE" if method == "GET" else "OUTCOME_UNKNOWN",
                "MinerU operation outcome unconfirmed",
            ) from error
        if response.status_code in {400, 401, 403, 422}:
            raise ExecutionError(
                Code.PARSER_CONFIGURATION_ERROR, "PERMANENT", "MinerU request rejected"
            )
        if response.status_code == 429 or (method == "GET" and response.is_server_error):
            raise ExecutionError(Code.PARSER_UNAVAILABLE, "LOCAL_RETRYABLE", "MinerU unavailable")
        if not response.is_success:
            raise ExecutionError(
                Code.EXECUTION_INTERNAL_ERROR, "OUTCOME_UNKNOWN", "MinerU outcome unconfirmed"
            )
        try:
            value = response.json()
            if not isinstance(value, dict) or type(value.get("code")) not in {str, int}:
                raise ValueError("Missing business code")
            code = value["code"]
            if code != 0:
                if code in {-60001, -60009}:
                    raise ExecutionError(
                        Code.PARSER_UNAVAILABLE, "LOCAL_RETRYABLE", "MinerU request not accepted"
                    )
                if code in {"A0202", "A0211", -500, -10002, -60013, -60018, -60019}:
                    raise ExecutionError(
                        Code.PARSER_CONFIGURATION_ERROR, "PERMANENT", "MinerU request rejected"
                    )
                if code in {-60005, -60006}:
                    raise ExecutionError(
                        Code.DOCUMENT_PARSE_RESOURCE_LIMIT, "PERMANENT", "MinerU resource limit"
                    )
                if code in {-60002, -60003, -60004, -60010, -60015, -60016, -60017}:
                    raise ExecutionError(
                        Code.DOCUMENT_PARSE_FAILED, "PERMANENT", "MinerU rejected the original"
                    )
                raise ExecutionError(
                    Code.EXECUTION_INTERNAL_ERROR, "OUTCOME_UNKNOWN", "MinerU outcome unconfirmed"
                )
            if not isinstance(value.get("data"), dict):
                raise ValueError("Missing response data")
            return value["data"]
        except (ValueError, TypeError) as error:
            # A malformed submission response does not prove the request was not accepted.
            raise ExecutionError(
                Code.EXECUTION_INTERNAL_ERROR, "OUTCOME_UNKNOWN", "MinerU outcome unconfirmed"
            ) from error

    async def request_upload(self) -> UploadBatch:
        data = await self._api(
            "POST",
            "/api/v4/file-urls/batch",
            body={
                "files": [{"name": "source.pdf", "is_ocr": False}],
                "model_version": "vlm",
                "enable_table": True,
                "enable_formula": True,
                "language": "ch",
            },
        )
        try:
            return UploadBatch.model_validate(data)
        except ValidationError as error:
            raise ExecutionError(
                Code.EXECUTION_INTERNAL_ERROR, "OUTCOME_UNKNOWN", "Upload allocation unconfirmed"
            ) from error

    async def upload_original(self, url: HttpUrl, raw: bytes) -> None:
        try:
            response = await self.objects.put(
                str(url), content=raw, timeout=self.settings.request_timeout_seconds
            )
            response.raise_for_status()
        except httpx.HTTPError as error:
            raise ExecutionError(
                Code.EXECUTION_INTERNAL_ERROR, "OUTCOME_UNKNOWN", "Original upload unconfirmed"
            ) from error

    async def get_result(self, batch_id: str) -> ExtractResult:
        data = await self._api("GET", "/api/v4/extract-results/batch/" + quote(batch_id, safe=""))
        try:
            result = _BatchResult.model_validate(data)
            if result.batch_id != batch_id:
                raise ValueError("Batch mismatch")
            return result.extract_result[0]
        except (ValidationError, ValueError) as error:
            raise _invalid() from error

    async def download_result(self, url: HttpUrl) -> bytes:
        try:
            async with self.objects.stream(
                "GET", str(url), timeout=self.settings.request_timeout_seconds
            ) as response:
                response.raise_for_status()
                data = bytearray()
                async for part in response.aiter_bytes():
                    if len(data) + len(part) > self.settings.max_result_bytes:
                        raise ExecutionError(
                            Code.DOCUMENT_PARSE_RESOURCE_LIMIT, "PERMANENT", "Result exceeds limit"
                        )
                    data.extend(part)
                return bytes(data)
        except httpx.HTTPError as error:
            raise ExecutionError(
                Code.PARSER_UNAVAILABLE, "LOCAL_RETRYABLE", "Result download unavailable"
            ) from error
