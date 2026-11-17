"""HTTP business data and execution exceptions have distinct consumers."""

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict


class BusinessError[Code: StrEnum](BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: Code
    stage: str
    retryable: bool
    user_message: str
    detail_ref: str | None = None


type ErrorCategory = Literal[
    "LOCAL_RETRYABLE", "TASK_RETRYABLE", "PERMANENT", "OUTCOME_UNKNOWN", "ABORT"
]


class ExecutionError(Exception):
    def __init__(self, code: str, category: ErrorCategory, message: str):
        super().__init__(message)
        self.code = code
        self.category = category
        self.message = message
