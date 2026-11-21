"""Receive and check uploads before business registration or queuing."""

import logging
import time
from typing import BinaryIO
from uuid import uuid4

from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.scanner.domain.errors import UploadFailure
from intelligent_reimbursement_assitant.scanner.domain.file_checks import (
    validate_detected_type,
    validate_original_name,
)
from intelligent_reimbursement_assitant.scanner.domain.models import (
    QuarantinedUpload,
    ScanReport,
    ScanVerdict,
    UploadPolicy,
)
from intelligent_reimbursement_assitant.scanner.ports.artifact_store import ArtifactStorePort
from intelligent_reimbursement_assitant.scanner.ports.format_probe import FileCheckPort
from intelligent_reimbursement_assitant.scanner.ports.scanner import ScannerPort

logger = logging.getLogger(__name__)


class UploadIntake:
    def __init__(self, store: ArtifactStorePort, checker: FileCheckPort, scanner: ScannerPort):
        self.store = store
        self.checker = checker
        self.scanner = scanner

    def receive_and_scan(
        self,
        stream: BinaryIO,
        original_name: str,
        policy: UploadPolicy,
        *,
        deadline: float,
    ) -> tuple[QuarantinedUpload, ScanReport]:
        validate_original_name(original_name)
        temp_key, size, sha256 = self.store.stage_stream(
            stream, max_bytes=policy.max_file_bytes, deadline=deadline
        )
        # One deadline starts at completed staging; no phase resets this budget.
        check_deadline = time.monotonic() + policy.scan_timeout_seconds
        detail_ref = uuid4().hex
        started = time.monotonic()
        try:
            with self.store.open_read(temp_key):
                media = self.checker.check_file(temp_key, policy, deadline=check_deadline)
            validate_detected_type(original_name, media, policy)
            upload = QuarantinedUpload(
                temp_key=temp_key,
                original_name=original_name,
                detected_type=media,
                size_bytes=size,
                sha256=sha256,
            )
            report = self.scanner.scan(upload, deadline=check_deadline, detail_ref=detail_ref)
            if report.verdict != ScanVerdict.CLEAN:
                self._raise_scan_failure(report, detail_ref)
            if time.monotonic() >= check_deadline:
                raise UploadFailure(
                    ErrorCode.SCAN_TIMEOUT,
                    "文件检查与扫描超时，请重试。",
                    stage="upload_checks",
                    retryable=True,
                    detail_ref=detail_ref,
                )
            logger.info(
                "upload_checked stage=upload_checks temp_key=%s detail_ref=%s size_bytes=%d elapsed_ms=%d",
                temp_key,
                detail_ref,
                size,
                int((time.monotonic() - started) * 1000),
            )
            return upload, report
        except BaseException as exc:
            self.discard_after_failure(temp_key)
            if isinstance(exc, UploadFailure):
                logger.warning(
                    "upload_check_failed stage=upload_checks temp_key=%s detail_ref=%s code=%s elapsed_ms=%d",
                    temp_key,
                    detail_ref,
                    exc.error.code,
                    int((time.monotonic() - started) * 1000),
                )
                raise UploadFailure(
                    exc.error.code,
                    exc.error.user_message,
                    stage="upload_checks",
                    retryable=exc.error.retryable,
                    detail_ref=exc.error.detail_ref,
                ) from exc
            raise

    @staticmethod
    def _raise_scan_failure(report: ScanReport, detail_ref: str) -> None:
        reasons = set(report.reason_codes)
        if report.verdict == ScanVerdict.REJECTED:
            policy_rejection = bool(reasons & {"ENCRYPTED_FILE", "MACROS_DETECTED"})
            code = ErrorCode.FILE_POLICY_REJECTED if policy_rejection else ErrorCode.SCAN_REJECTED
            message = (
                "不接收加密文件或含宏的文件。"
                if policy_rejection
                else "扫描发现恶意内容，文件已拒绝。"
            )
            retryable = False
        elif reasons & {"SCAN_RESOURCE_LIMIT", "SCAN_STREAM_TOO_LARGE"}:
            code, message, retryable = (
                ErrorCode.SCAN_RESOURCE_LIMIT,
                "扫描超过资源限制，请调整文件或扫描环境后重试。",
                True,
            )
        elif "SCAN_TIMEOUT" in reasons:
            code, message, retryable = ErrorCode.SCAN_TIMEOUT, "扫描超时，请重试。", True
        elif "SCANNER_UNAVAILABLE" in reasons:
            code, message, retryable = (
                ErrorCode.SCAN_UNAVAILABLE,
                "扫描服务暂不可用，请稍后重试。",
                True,
            )
        else:
            code, message, retryable = (
                ErrorCode.SCAN_INCOMPLETE,
                "扫描未能完整完成，请检查扫描环境后重试。",
                True,
            )
        raise UploadFailure(
            code, message, stage="upload_checks", retryable=retryable, detail_ref=detail_ref
        )

    def discard_after_failure(self, temp_key: str) -> None:
        try:
            self.store.discard(temp_key)
        except OSError:
            logger.error("quarantine_cleanup_failed stage=upload_checks temp_key=%s", temp_key)
