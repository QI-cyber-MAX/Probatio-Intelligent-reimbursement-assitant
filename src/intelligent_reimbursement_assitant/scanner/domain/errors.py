"""Upload and scan failures retain their actual stage and diagnostic reference."""

from typing import Literal

from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import BusinessError


class UploadFailure(Exception):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        stage: Literal[
            "upload_reception", "upload_checks", "document_registration"
        ] = "upload_reception",
        detail_ref: str | None = None,
    ):
        self.error = BusinessError[ErrorCode](
            code=code, stage=stage, retryable=retryable, user_message=message, detail_ref=detail_ref
        )
        super().__init__(message)
