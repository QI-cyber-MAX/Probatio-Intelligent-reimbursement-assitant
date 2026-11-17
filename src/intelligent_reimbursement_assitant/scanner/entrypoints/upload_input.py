"""Reusable FastAPI request entry, without inventing a business upload API."""

import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI, Request
from python_multipart.multipart import parse_options_header
from starlette.datastructures import FormData, UploadFile
from starlette.formparsers import MultiPartException, MultiPartParser
from starlette.requests import ClientDisconnect

from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.http import business_error_response
from intelligent_reimbursement_assitant.scanner.application.upload_intake import UploadIntake
from intelligent_reimbursement_assitant.scanner.domain.errors import UploadFailure
from intelligent_reimbursement_assitant.scanner.domain.models import (
    QuarantinedUpload,
    ScanReport,
    UploadPolicy,
)
from intelligent_reimbursement_assitant.scanner.entrypoints.body_limit import (
    UploadBodyLimit,
)

logger = logging.getLogger(__name__)


def install_upload_input(
    app: FastAPI,
    *,
    paths: set[str],
    policy: UploadPolicy,
    max_concurrent_uploads: int = 2,
    slot_wait_seconds: float = 2,
    reception_timeout_seconds: float = 120,
) -> None:
    """Register admission before routing and the fixed BusinessError HTTP mapping."""
    app.add_middleware(
        UploadBodyLimit,
        paths=paths,
        policy=policy,
        max_concurrent_uploads=max_concurrent_uploads,
        slot_wait_seconds=slot_wait_seconds,
        reception_timeout_seconds=reception_timeout_seconds,
    )

    async def handle_failure(request: Request, exception: Exception):
        return business_error_response(cast(UploadFailure, exception).error)

    app.add_exception_handler(UploadFailure, handle_failure)


class _CompleteMultipartParser(MultiPartParser):
    """Starlette's parser accepts some truncated streams; require its final boundary."""

    complete = False

    def on_end(self) -> None:
        self.complete = True

    def _close_failed_parts(self) -> None:
        for file in self._files_to_close_on_error:
            try:
                file.close()
            except OSError:
                logger.error("framework_upload_cleanup_failed stage=D01")

    async def parse(self) -> FormData:
        try:
            form = await super().parse()
        except BaseException as exc:
            self._close_failed_parts()
            # In Starlette 1.7.0 an OSError from close() inside its exception
            # handler can replace the active request failure. Python preserves
            # that failure as __context__; retain the defined primary errors.
            if isinstance(exc, OSError) and isinstance(
                exc.__context__,
                (UploadFailure, MultiPartException, ClientDisconnect, asyncio.CancelledError),
            ):
                raise exc.__context__ from None
            raise
        if not self.complete:
            # Starlette 1.7.0 only adds finished parts to FormData. The parser's
            # owned file list also includes a part truncated before on_part_end.
            self._close_failed_parts()
            raise MultiPartException("Missing final multipart boundary.")
        return form


def _close_form(form: FormData) -> None:
    primary_error = sys.exception()
    close_error: OSError | None = None
    for _, item in form.multi_items():
        if isinstance(item, UploadFile) and not item.file.closed:
            try:
                item.file.close()
            except OSError as exc:
                close_error = exc
                logger.error("framework_upload_cleanup_failed stage=D01")
    if close_error is not None and primary_error is None:
        raise UploadFailure(
            ErrorCode.UPLOAD_IO_ERROR, "上传临时文件关闭失败。", retryable=True
        ) from close_error


@asynccontextmanager
async def parsed_upload_input(
    request: Request, *, text_fields: set[str]
) -> AsyncIterator[tuple[UploadFile, dict[str, str]]]:
    """Parse once under admission limits; the caller interprets business text."""
    if "d01.receive_deadline" not in request.scope or "d01.upload_policy" not in request.scope:
        raise RuntimeError("parsed_upload_input requires UploadBodyLimit on this path")
    media, _ = parse_options_header(request.headers.get("content-type"))
    if media != b"multipart/form-data":
        raise UploadFailure(ErrorCode.INVALID_UPLOAD, "请使用单文件 multipart/form-data 上传。")
    parser = _CompleteMultipartParser(
        request.headers,
        request.stream(),
        max_files=1,
        max_fields=len(text_fields),
        max_part_size=1024,
    )
    try:
        form = await parser.parse()
    except (MultiPartException, ClientDisconnect) as exc:
        raise UploadFailure(ErrorCode.INVALID_UPLOAD, "上传请求无效或接收已中断。") from exc
    try:
        fields: dict[str, str] = {}
        file: UploadFile | None = None
        seen: set[str] = set()
        for name, value in form.multi_items():
            if name in seen or name not in text_fields | {"file"}:
                raise UploadFailure(ErrorCode.INVALID_UPLOAD, "上传字段未知或重复。")
            seen.add(name)
            if name == "file":
                if not isinstance(value, UploadFile) or not value.filename:
                    raise UploadFailure(ErrorCode.INVALID_UPLOAD, "上传请求缺少有效文件名。")
                file = value
            elif not isinstance(value, str):
                raise UploadFailure(ErrorCode.INVALID_UPLOAD, "登记字段必须为文本。")
            else:
                fields[name] = value
        if file is None:
            raise UploadFailure(ErrorCode.INVALID_UPLOAD, "上传请求缺少 file 文件部分。")
        yield file, fields
    finally:
        _close_form(form)


@asynccontextmanager
async def scanned_upload_input(
    request: Request, intake: UploadIntake, file: UploadFile
) -> AsyncIterator[tuple[QuarantinedUpload, ScanReport]]:
    """Close the framework spool before document registration; always clean quarantine after use."""
    worker = asyncio.create_task(
        asyncio.to_thread(
            intake.receive_and_scan,
            file.file,
            cast(str, file.filename),
            request.scope["d01.upload_policy"],
            deadline=request.scope["d01.receive_deadline"],
        )
    )
    upload: QuarantinedUpload | None = None
    try:
        try:
            received = await asyncio.shield(worker)
            upload, _ = received
        except BaseException:
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if worker.done() and not worker.cancelled() and worker.exception() is None:
                upload, _ = worker.result()
            raise
        try:
            file.file.close()
        except OSError as exc:
            logger.error("framework_upload_cleanup_failed stage=D01")
            raise UploadFailure(
                ErrorCode.UPLOAD_IO_ERROR, "上传临时文件关闭失败。", retryable=True
            ) from exc
        yield received
    finally:
        if upload is not None:
            intake.discard_after_failure(upload.temp_key)
