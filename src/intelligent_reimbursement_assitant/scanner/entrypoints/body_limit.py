"""ASGI admission and byte counting, outside multipart parsing."""

import asyncio
import math
import time

from starlette.routing import compile_path
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.http import business_error_response
from intelligent_reimbursement_assitant.scanner.domain.errors import UploadFailure
from intelligent_reimbursement_assitant.scanner.domain.models import UploadPolicy


class UploadBodyLimit:
    """Install for the caller's upload paths. Slots cover the entire downstream call.

    Paths may be literals or Starlette route templates such as
    ``/knowledge-bases/{kb_id}/documents``. Match the whole request path.

    The caller validates the knowledge base before receiving the file; reception,
    scanning and document registration all run inside this guarded call.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        paths: set[str],
        policy: UploadPolicy,
        max_concurrent_uploads: int = 2,
        slot_wait_seconds: float = 2,
        reception_timeout_seconds: float = 120,
    ):
        if (
            type(max_concurrent_uploads) is not int
            or max_concurrent_uploads <= 0
            or not math.isfinite(slot_wait_seconds)
            or slot_wait_seconds <= 0
            or not math.isfinite(reception_timeout_seconds)
            or reception_timeout_seconds <= 0
        ):
            raise ValueError("upload execution limits must be positive")
        self.app = app
        self.path_regexes = tuple(compile_path(path)[0] for path in paths)
        self.policy = policy.model_copy(deep=True)
        self.slots = asyncio.Semaphore(max_concurrent_uploads)
        self.slot_wait_seconds = slot_wait_seconds
        self.reception_timeout_seconds = reception_timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or not any(pattern.fullmatch(scope["path"]) for pattern in self.path_regexes)
        ):
            await self.app(scope, receive, send)
            return
        acquired = False
        response_started = False

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            lengths = [value for key, value in scope["headers"] if key.lower() == b"content-length"]
            if lengths:
                if len(lengths) != 1 or not lengths[0].isdigit():
                    raise UploadFailure(ErrorCode.INVALID_UPLOAD, "请求长度头无效。")
                actual_length = lengths[0].lstrip(b"0") or b"0"
                maximum = str(self.policy.max_request_bytes).encode("ascii")
                if len(actual_length) > len(maximum) or (
                    len(actual_length) == len(maximum) and actual_length > maximum
                ):
                    raise UploadFailure(ErrorCode.UPLOAD_TOO_LARGE, "上传请求超过大小上限。")
            try:
                async with asyncio.timeout(self.slot_wait_seconds):
                    await self.slots.acquire()
            except TimeoutError as exc:
                raise UploadFailure(
                    ErrorCode.UPLOAD_BUSY, "上传服务繁忙，请稍后重试。", retryable=True
                ) from exc
            acquired = True
            deadline = time.monotonic() + self.reception_timeout_seconds
            scope["upload.receive_deadline"] = deadline
            scope["upload.policy"] = self.policy
            total = 0

            async def limited_receive() -> Message:
                nonlocal total
                if time.monotonic() >= deadline:
                    raise UploadFailure(
                        ErrorCode.UPLOAD_TIMEOUT, "上传接收超时，请重试。", retryable=True
                    )
                try:
                    async with asyncio.timeout(max(0, deadline - time.monotonic())):
                        message = await receive()
                except TimeoutError as exc:
                    raise UploadFailure(
                        ErrorCode.UPLOAD_TIMEOUT, "上传接收超时，请重试。", retryable=True
                    ) from exc
                if message["type"] == "http.request":
                    total += len(message.get("body", b""))
                    if total > self.policy.max_request_bytes:
                        raise UploadFailure(ErrorCode.UPLOAD_TOO_LARGE, "上传请求超过大小上限。")
                return message

            await self.app(scope, limited_receive, tracked_send)
        except UploadFailure as exc:
            if response_started:
                raise
            await business_error_response(exc.error)(scope, receive, send)
        finally:
            if acquired:
                self.slots.release()
