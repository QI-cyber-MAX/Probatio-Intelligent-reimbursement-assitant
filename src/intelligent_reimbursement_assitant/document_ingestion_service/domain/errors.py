from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import BusinessError


class RegistrationFailure(Exception):
    def __init__(self, code: ErrorCode, message: str, *, retryable: bool = False):
        self.error = BusinessError[ErrorCode](
            code=code,
            stage="document_registration",
            retryable=retryable,
            user_message=message,
        )
        super().__init__(message)
