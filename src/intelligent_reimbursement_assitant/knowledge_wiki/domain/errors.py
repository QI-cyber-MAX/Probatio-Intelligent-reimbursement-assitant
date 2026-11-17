from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import BusinessError


class WikiFailure(Exception):
    def __init__(self, code: ErrorCode, message: str, *, retryable: bool = False):
        self.error = BusinessError[ErrorCode](
            code=code,
            stage="WIKI",
            retryable=retryable,
            user_message=message,
        )
        super().__init__(message)


class WikiEditValidationError(Exception):
    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__("Invalid Wiki edit")
