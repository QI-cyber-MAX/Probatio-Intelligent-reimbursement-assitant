"""Maintenance process entrypoint; shared configuration is loaded exactly once."""

import asyncio
import logging
from typing import cast

from pydantic import SecretStr

from intelligent_reimbursement_assitant.dispatcher.entrypoints.maintenance import _serve
from intelligent_reimbursement_assitant.entrypoints.configuration import (
    load_settings,
    queue_binding,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import ExecutionKind


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    settings = load_settings("maintenance")
    bindings = [queue_binding(settings.queue_groups, kind) for kind in ExecutionKind]
    asyncio.run(
        _serve(
            settings.database_url.get_secret_value(),
            cast(SecretStr, settings.redis_url).get_secret_value(),
            settings.maintenance,
            {binding.kind: (binding.stream, binding.group) for binding in bindings},
            settings.wiki,
        )
    )


if __name__ == "__main__":
    main()
