"""Production ingestion process; load the shared role configuration exactly once."""

import asyncio
import logging
from functools import partial
from pathlib import Path
from typing import cast

from pydantic import SecretStr

from intelligent_reimbursement_assitant.entrypoints.configuration import (
    AppSettings,
    load_settings,
    queue_binding,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import ExecutionKind
from intelligent_reimbursement_assitant.ingestion_executor.domain.config import IngestionSettings
from intelligent_reimbursement_assitant.ingestion_executor.entrypoints.worker import (
    ingestion_stage_factory,
)
from intelligent_reimbursement_assitant.worker_runtime.entrypoints.worker import serve_worker


async def _serve(settings: AppSettings) -> None:
    ingestion = cast(IngestionSettings, settings.ingestion)
    directory = cast(Path, settings.managed_directory)
    await serve_worker(
        settings.database_url.get_secret_value(),
        cast(SecretStr, settings.redis_url).get_secret_value(),
        directory,
        partial(
            ingestion_stage_factory,
            settings=ingestion,
            managed_directory=directory,
            index_profiles=settings.index_profiles,
        ),
        ingestion.worker,
        binding=queue_binding(settings.queue_groups, ExecutionKind.INGESTION),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # Signed object URLs and HTTP authorization details are not application diagnostics.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    asyncio.run(_serve(load_settings("ingestion")))


if __name__ == "__main__":
    main()
