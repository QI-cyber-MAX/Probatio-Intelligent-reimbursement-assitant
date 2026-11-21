from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import BusinessError


class QAFailure(Exception):
    def __init__(
        self, code: ErrorCode, message: str, *, retryable: bool = False, stage: str = "qa"
    ):
        self.error = BusinessError[ErrorCode](
            code=code, stage=stage, retryable=retryable, user_message=message
        )
        super().__init__(message)
