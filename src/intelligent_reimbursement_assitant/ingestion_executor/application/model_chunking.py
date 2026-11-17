"""One semantic grouping request with accounting before semantic validation."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, JsonValue, ValidationError

from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import ChunkBatch
from intelligent_reimbursement_assitant.errors.codes import ErrorCode as Code
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.errors.execution import retry_safe_operation
from intelligent_reimbursement_assitant.ingestion_executor.domain.chunking_content import (
    assemble_chunks,
)
from intelligent_reimbursement_assitant.ingestion_executor.domain.config import ChunkingSettings
from intelligent_reimbursement_assitant.ingestion_executor.domain.model_chunking import (
    GroupsResponse,
    build_model_input,
    build_sections,
    build_units,
    invalid_response,
    render_group,
    validate_model_groups,
)
from intelligent_reimbursement_assitant.ingestion_executor.domain.normalization import (
    NormalizedDocument,
)
from intelligent_reimbursement_assitant.model_gateway.ports.model_gateway import ModelGateway

logger = logging.getLogger(__name__)


class _Message(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    content: str


class _Choice(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    finish_reason: str
    message: _Message


async def run_model_chunking(
    normalized: NormalizedDocument,
    *,
    document_id: UUID,
    source_hash: str,
    generation_id: UUID,
    settings: ChunkingSettings,
    count_tokens: Callable[[str], int],
    count_model_tokens: Callable[[str], int],
    gateway: ModelGateway,
    check_execution: Callable[[], Awaitable[bool]],
    mark_started: Callable[[bool], Awaitable[None]],
    record_usage: Callable[[int], Awaitable[None]],
) -> ChunkBatch:
    async def compute(operation):
        pending = asyncio.create_task(asyncio.to_thread(operation))
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            await pending
            raise

    model = settings.model
    if model is None:
        raise ExecutionError(
            Code.CHUNKING_MODEL_CONFIGURATION_ERROR, "PERMANENT", "Chunking model is not configured"
        )

    def prepare():
        sections = build_sections(normalized)
        units = build_units(normalized, sections, settings, count_tokens)
        messages = build_model_input(normalized, sections, units, settings, count_tokens)
        input_tokens = count_model_tokens(
            json.dumps(
                {"messages": messages, "response_format": {"type": "json_object"}},
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        logger.info(
            "chunking_model_input sections=%s units=%s input_tokens=%s input_limit=%s",
            len(sections),
            len(units),
            input_tokens,
            settings.model_input_token_limit,
        )
        if (
            input_tokens > settings.model_input_token_limit
            or input_tokens + model.max_output_tokens > model.context_window_tokens
        ):
            raise ExecutionError(
                Code.CHUNKING_CONTENT_TOO_LARGE,
                "PERMANENT",
                "Complete chunking request exceeds model input capacity",
            )
        return sections, units, messages

    sections, units, messages = await compute(prepare)

    async def request() -> dict[str, JsonValue]:
        await mark_started(True)

        async def settle():
            try:
                raw, usage = await gateway.generate_structured(
                    messages, GroupsResponse.model_json_schema()
                )
            except ExecutionError as error:
                if error.category == "LOCAL_RETRYABLE":
                    # Clear the external-call interval only after a definite non-execution.
                    await mark_started(False)
                raise
            logger.info(
                "chunking_model_usage input_tokens=%s output_tokens=%s elapsed_ms=%s",
                usage.input_tokens,
                usage.output_tokens,
                usage.elapsed_ms,
            )
            if usage.input_tokens is None:
                raise ExecutionError(
                    Code.CHUNKING_MODEL_OUTCOME_UNKNOWN,
                    "OUTCOME_UNKNOWN",
                    "Chunking model input usage unconfirmed",
                )
            await record_usage(usage.input_tokens)
            return raw

        task = asyncio.create_task(settle())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            await task
            raise

    raw = await retry_safe_operation(request, check_execution)
    if not await check_execution():
        raise asyncio.CancelledError

    def validate():
        constraint = "native_envelope"
        try:
            choices = raw["choices"]
            if not isinstance(choices, list) or len(choices) != 1:
                raise ValueError("Expected one native choice")
            choice = _Choice.model_validate(choices[0])
            if choice.finish_reason != "stop":
                raise ValueError("Incomplete model output")
            constraint = "json_schema"
            response = GroupsResponse.model_validate_json(choice.message.content)
            constraint = "body_coverage"
            groups = validate_model_groups(response, sections, units)
            section_map = {s.section_id: s for s in sections}
            by_id = {b.block_id: b for b in normalized.blocks}
            rendered = []
            constraint = "final_length"
            for body, context in groups:
                part = render_group(
                    body,
                    context,
                    section_map[body[0].section_id],
                    by_id,
                )
                if max(count_tokens(part.body), count_tokens(part.retrieval)) > settings.max_tokens:
                    raise ValueError("Rendered group exceeds token limit")
                rendered.append(part)
            return assemble_chunks(rendered, document_id, source_hash, generation_id)
        except (ValueError, KeyError, ValidationError, ExecutionError) as error:
            logger.warning("chunking_model_response_invalid constraint=%s", constraint)
            raise invalid_response() from error

    return await compute(validate)
