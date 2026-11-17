"""Reception checks independent of framework and file format libraries."""

import unicodedata
from pathlib import PurePosixPath

from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.scanner.domain.errors import UploadFailure
from intelligent_reimbursement_assitant.scanner.domain.models import (
    DOC,
    DOCX,
    PDF,
    XLSX,
    UploadPolicy,
)


def validate_original_name(original_name: str) -> None:
    if (
        not original_name.strip()
        or len(original_name) > 255
        or any(unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in original_name)
    ):
        raise UploadFailure(ErrorCode.INVALID_UPLOAD, "请提供有效且不超过 255 字符的文件名。")


def validate_detected_type(original_name: str, media_type: str, policy: UploadPolicy) -> None:
    if media_type not in policy.allowed_media_types:
        raise UploadFailure(ErrorCode.UNSUPPORTED_FILE_TYPE, "不支持该文件的实际格式。")
    extension = PurePosixPath(original_name.replace("\\", "/")).suffix.lower()
    expected = {".pdf": PDF, ".docx": DOCX, ".xlsx": XLSX, ".doc": DOC}.get(extension)
    if extension and expected != media_type:
        raise UploadFailure(ErrorCode.FILE_TYPE_MISMATCH, "文件扩展名与实际格式不一致。")
