"""One cloud parsing call; accepted remote work is never resubmitted implicitly."""

import asyncio
import errno
import logging
from collections.abc import Awaitable, Callable
from time import monotonic
from uuid import UUID

from intelligent_reimbursement_assitant.contracts.models import ArtifactRef
from intelligent_reimbursement_assitant.errors.codes import ErrorCode as Code
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.errors.execution import retry_safe_operation
from intelligent_reimbursement_assitant.parser.adapters.mineru.client import MinerUClient
from intelligent_reimbursement_assitant.parser.adapters.mineru.mapping import (
    map_document,
    read_result_archive,
)
from intelligent_reimbursement_assitant.parser.domain.config import ParserSettings
from intelligent_reimbursement_assitant.parser.domain.models import ParsedDocument
from intelligent_reimbursement_assitant.scanner.ports.artifact_store import ArtifactStorePort

logger = logging.getLogger(__name__)


class MinerUParserAdapter:
    def __init__(self, store: ArtifactStorePort, settings: ParserSettings, client: MinerUClient):
        self.store = store
        self.settings = settings
        self.client = client

    async def aclose(self) -> None:
        # HTTP clients are owned by the factory's AsyncExitStack.
        pass

    async def parse(
        self,
        document_id: UUID,
        source_ref: ArtifactRef,
        *,
        check_execution: Callable[[], Awaitable[bool]],
    ) -> ParsedDocument:
        remote_possible = False

        async def permitted():
            if not await check_execution():
                raise asyncio.CancelledError
            return True

        def read_original():
            try:
                with self.store.open_artifact(source_ref) as source:
                    return source.read()
            except (OSError, ValueError) as error:
                temporary = isinstance(error, OSError) and error.errno in {
                    errno.EAGAIN,
                    errno.EBUSY,
                    errno.EINTR,
                    errno.ETIMEDOUT,
                    errno.EIO,
                }
                raise ExecutionError(
                    Code.PARSING_ARTIFACT_ERROR,
                    "LOCAL_RETRYABLE" if temporary else "PERMANENT",
                    "Parser original could not be read",
                ) from error

        async def settled(operation):
            pending = asyncio.create_task(asyncio.to_thread(operation))
            try:
                return await asyncio.shield(pending)
            except asyncio.CancelledError:
                await pending
                raise

        try:
            async with asyncio.timeout(self.settings.timeout_seconds):
                await permitted()
                raw = await settled(read_original)
                await permitted()
                phase_started = monotonic()
                remote_possible = True
                try:
                    batch = await self.client.request_upload()
                except ExecutionError as error:
                    if error.category == "LOCAL_RETRYABLE":
                        remote_possible = False
                    raise
                logger.info("mineru_batch_allocated batch_id=%s", batch.batch_id)
                await permitted()
                await self.client.upload_original(batch.file_urls[0], raw)
                logger.info(
                    "mineru_phase_completed batch_id=%s phase=upload elapsed_seconds=%.3f",
                    batch.batch_id,
                    monotonic() - phase_started,
                )
                phase_started = monotonic()
                while True:
                    result = await retry_safe_operation(
                        lambda: self.client.get_result(batch.batch_id), permitted
                    )
                    if result.state == "failed":
                        # Free-form provider text is not a retry classifier or a public reason.
                        logger.warning("mineru_parse_failed batch_id=%s", batch.batch_id)
                        raise ExecutionError(
                            Code.DOCUMENT_PARSE_FAILED,
                            "PERMANENT",
                            "MinerU parsing failed: " + result.err_msg,
                        )
                    if result.state == "done":
                        break
                    await asyncio.sleep(self.client.settings.poll_interval_seconds)
                logger.info(
                    "mineru_phase_completed batch_id=%s phase=wait elapsed_seconds=%.3f",
                    batch.batch_id,
                    monotonic() - phase_started,
                )
                if result.full_zip_url is None:
                    raise ExecutionError(
                        Code.PARSE_RESULT_INVALID, "PERMANENT", "MinerU result URL missing"
                    )
                result_url = result.full_zip_url
                phase_started = monotonic()
                archive = await retry_safe_operation(
                    lambda: self.client.download_result(result_url), permitted
                )
                logger.info(
                    "mineru_phase_completed batch_id=%s phase=download elapsed_seconds=%.3f",
                    batch.batch_id,
                    monotonic() - phase_started,
                )
                phase_started = monotonic()
                layout = await settled(lambda: read_result_archive(archive, self.client.settings))
                document = await settled(lambda: map_document(layout, document_id, source_ref, raw))
                logger.info(
                    "mineru_phase_completed batch_id=%s phase=mapping elapsed_seconds=%.3f",
                    batch.batch_id,
                    monotonic() - phase_started,
                )
                await permitted()
                return document
        except TimeoutError as error:
            raise ExecutionError(
                Code.EXECUTION_INTERNAL_ERROR if remote_possible else Code.DOCUMENT_PARSE_TIMEOUT,
                "OUTCOME_UNKNOWN" if remote_possible else "TASK_RETRYABLE",
                "Cloud parsing timeout; remote completion unconfirmed"
                if remote_possible
                else "Parsing timed out before submission",
            ) from error
        except asyncio.CancelledError as error:
            if remote_possible:
                raise ExecutionError(
                    Code.EXECUTION_INTERNAL_ERROR,
                    "OUTCOME_UNKNOWN",
                    "Cloud parsing stopped; remote completion unconfirmed",
                ) from error
            raise
        except ExecutionError as error:
            if remote_possible and error.code == Code.EXECUTION_CANCEL_REQUESTED:
                raise ExecutionError(
                    Code.EXECUTION_INTERNAL_ERROR,
                    "OUTCOME_UNKNOWN",
                    "Cloud parsing stopped; remote completion unconfirmed",
                ) from error
            if remote_possible and error.category == "TASK_RETRYABLE":
                raise ExecutionError(
                    Code.PARSER_UNAVAILABLE,
                    "OUTCOME_UNKNOWN",
                    "Accepted cloud result cannot be confirmed",
                ) from error
            raise
