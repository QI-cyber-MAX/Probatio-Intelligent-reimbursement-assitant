"""Shared execution-row ownership rules for state transitions and checkpoint writes."""

from collections.abc import Mapping
from datetime import datetime
from typing import Any, cast

from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionContext,
    ExecutionStatus,
)


def require_execution_owner(
    run: Mapping[Any, Any] | None,
    ctx: ExecutionContext,
    now: datetime,
    *,
    recovery: bool = False,
) -> None:
    valid = (
        run is not None
        and run["lease_epoch"] == ctx.lease_epoch
        and run["status"] in {ExecutionStatus.RUNNING, ExecutionStatus.CANCELLING}
        and run["lease_expires_at"] is not None
    )
    if valid:
        run = cast(Mapping[Any, Any], run)
        expires_at = cast(datetime, run["lease_expires_at"])
        valid = (
            run["lease_owner"] is None and expires_at <= now
            if recovery
            else run["lease_owner"] is not None and expires_at > now
        )
    if not valid:
        raise ExecutionError(
            ErrorCode.EXECUTION_LEASE_LOST, "ABORT", "Execution lease is no longer valid"
        )
