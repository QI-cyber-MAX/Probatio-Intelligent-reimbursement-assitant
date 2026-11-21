"""Bounded file checks, process lifecycle and result transport."""

import logging
import multiprocessing
import time
from multiprocessing.connection import Connection
from pathlib import Path

from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import BusinessError
from intelligent_reimbursement_assitant.scanner.adapters.container_checks import check_document
from intelligent_reimbursement_assitant.scanner.adapters.local_artifact_store import (
    LocalArtifactStore,
)
from intelligent_reimbursement_assitant.scanner.domain.errors import UploadFailure
from intelligent_reimbursement_assitant.scanner.domain.models import UploadPolicy

logger = logging.getLogger(__name__)


def _probe_worker(
    output: Connection,
    directory: Path,
    temp_key: str,
    policy: UploadPolicy,
) -> None:
    try:
        store = LocalArtifactStore(directory)
        with store.open_read(temp_key) as source:
            media = check_document(source, policy)
        output.send(("ok", media))
    except UploadFailure as exc:
        exc = UploadFailure(
            exc.error.code,
            exc.error.user_message,
            retryable=exc.error.retryable,
            stage="upload_checks",
            detail_ref=exc.error.detail_ref,
        )
        output.send(("error", exc.error.model_dump_json()))
    except Exception as exc:
        # Known format failures are normalized by the format adapters. Anything
        # reaching this process boundary is an execution failure, not proof of
        # damaged input. IO faults may be retried; unexpected bugs are not transient.
        logger.error(
            "format_probe_failed stage=upload_checks temp_key=%s error_type=%s",
            temp_key,
            type(exc).__name__,
        )
        error = UploadFailure(
            ErrorCode.UPLOAD_IO_ERROR,
            "文件检查执行失败。",
            stage="upload_checks",
            retryable=isinstance(exc, OSError),
        )
        output.send(("error", error.error.model_dump_json()))
    finally:
        output.close()


class FormatProbe:
    def __init__(self, quarantine_dir: Path):
        self.quarantine_dir = quarantine_dir

    def check_file(self, temp_key: str, policy: UploadPolicy, *, deadline: float) -> str:
        context = multiprocessing.get_context("spawn")
        reader, writer = context.Pipe(duplex=False)
        process = context.Process(
            target=_probe_worker,
            args=(writer, self.quarantine_dir, temp_key, policy),
        )
        started = False
        try:
            if time.monotonic() >= deadline:
                raise UploadFailure(
                    ErrorCode.SCAN_TIMEOUT,
                    "文件格式检查超时。",
                    retryable=True,
                    stage="upload_checks",
                )
            process.start()
            started = True
            writer.close()
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not reader.poll(remaining):
                raise UploadFailure(
                    ErrorCode.SCAN_TIMEOUT,
                    "文件格式检查超时。",
                    retryable=True,
                    stage="upload_checks",
                )
            status, payload = reader.recv()
            if status == "error":
                error = BusinessError[ErrorCode].model_validate_json(payload)
                raise UploadFailure(
                    error.code,
                    error.user_message,
                    retryable=error.retryable,
                    stage="upload_checks",
                    detail_ref=error.detail_ref,
                )
            if status != "ok":
                raise UploadFailure(
                    ErrorCode.UPLOAD_IO_ERROR, "文件检查进程结果无效。", stage="upload_checks"
                )
            return payload
        except (OSError, EOFError) as exc:
            raise UploadFailure(
                ErrorCode.UPLOAD_IO_ERROR,
                "文件格式检查进程不可用。",
                retryable=True,
                stage="upload_checks",
            ) from exc
        finally:
            if started:
                process.join(timeout=max(0, min(0.1, deadline - time.monotonic())))
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=0.5)
                if process.is_alive():
                    process.kill()
                    process.join()
            process.close()
            reader.close()
            writer.close()
