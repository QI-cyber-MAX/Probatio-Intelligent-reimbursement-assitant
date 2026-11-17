"""Document ingestion, immutable artifact recovery and atomic policy publication."""

import asyncio
import errno
import hashlib
from collections.abc import AsyncGenerator, Callable, Mapping
from contextlib import AsyncExitStack, aclosing
from functools import partial
from uuid import UUID, uuid5

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from intelligent_reimbursement_assitant.content_index.domain.config import IndexProfileSettings
from intelligent_reimbursement_assitant.content_index.ports.content_index import ContentIndexPort
from intelligent_reimbursement_assitant.contracts.models import ArtifactRef, SourceReference
from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    ChunkBatch,
    IndexFilterMetadata,
    IndexRecordBatch,
    PolicyDate,
    SourceMetadataUpdate,
    VersionStatus,
)
from intelligent_reimbursement_assitant.document_ingestion_service.domain.policy_metadata import (
    project_index_filter_metadata,
    resolve_policy_metadata,
)
from intelligent_reimbursement_assitant.document_ingestion_service.ports.repository import (
    DocumentRepository,
    IngestionSource,
)
from intelligent_reimbursement_assitant.embedding.ports.embedding import EmbeddingPort
from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.errors.execution import retry_safe_operation
from intelligent_reimbursement_assitant.execution_state_service.domain.events import (
    ProgressFactory,
    ProgressStage,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionContext,
    IngestionStage,
    WorkflowSucceeded,
)
from intelligent_reimbursement_assitant.ingestion_executor.application.model_chunking import (
    run_model_chunking,
)
from intelligent_reimbursement_assitant.ingestion_executor.domain.chunking import chunk_document
from intelligent_reimbursement_assitant.ingestion_executor.domain.config import ChunkingSettings
from intelligent_reimbursement_assitant.ingestion_executor.domain.embedding import (
    assemble_index_records,
)
from intelligent_reimbursement_assitant.ingestion_executor.domain.normalization import (
    NormalizedDocument,
    normalize_document,
)
from intelligent_reimbursement_assitant.ingestion_executor.domain.policy_dates import (
    extract_effective_date,
)
from intelligent_reimbursement_assitant.ingestion_executor.domain.publication import (
    compute_version_updates,
)
from intelligent_reimbursement_assitant.model_gateway.ports.model_gateway import ModelGateway
from intelligent_reimbursement_assitant.parser.domain.models import (
    CoverageStatus,
    DocumentBlock,
    ParsedDocument,
    TableBlock,
)
from intelligent_reimbursement_assitant.parser.ports.parser import ParserPort
from intelligent_reimbursement_assitant.scanner.ports.artifact_store import ArtifactStorePort


def _saved_effective_date(source: IngestionSource) -> PolicyDate | None:
    raw = source["checkpoint"].get("extracted_effective_from")
    return PolicyDate.model_validate(raw) if raw is not None else None


class _BudgetStopped(Exception):
    """The state transaction has already committed terminal budget exhaustion."""


def decide_parsing_handoff(document: ParsedDocument) -> ExecutionError | None:
    report = document.parse_report
    if report.coverage_status != CoverageStatus.COMPLETE or report.failed_locations:
        return ExecutionError(
            ErrorCode.DOCUMENT_PARSE_INCOMPLETE,
            "PERMANENT",
            f"Parsing coverage is {report.coverage_status}; manual review required",
        )
    if not any(
        (isinstance(block, DocumentBlock) and block.text.strip())
        or (isinstance(block, TableBlock) and any(cell.text.strip() for cell in block.cells))
        for block in document.blocks
    ):
        return ExecutionError(
            ErrorCode.DOCUMENT_PARSE_EMPTY, "PERMANENT", "Document has no ingestible content"
        )
    return None


async def _settled_work[T](operation: Callable[[], T]) -> T:
    """Keep the event loop free and settle owned work before exposing cancellation."""
    task = asyncio.create_task(asyncio.to_thread(operation))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # Runtime sends one cooperative stop request and keeps renewing during cleanup.
        await task
        raise


class IngestionExecutor:
    def __init__(
        self,
        repository: DocumentRepository,
        store: ArtifactStorePort,
        parser: ParserPort,
        check_execution: Callable[[Session, ExecutionContext], bool],
        chunking_settings: ChunkingSettings,
        count_tokens: Callable[[str], int],
        *,
        complete: Callable[[Session, ExecutionContext], None],
        index_profiles: Mapping[str, IndexProfileSettings] | None = None,
        embeddings: Mapping[str, EmbeddingPort] | None = None,
        content_indexes: Mapping[str, ContentIndexPort] | None = None,
        record_model_usage: Callable[[Session, UUID, int], None] | None = None,
        progress: ProgressFactory | None = None,
        chunking_model: ModelGateway | None = None,
        count_model_tokens: Callable[[str], int] | None = None,
    ):
        self.repository = repository
        self.store = store
        self.parser = parser
        self.check_execution = check_execution
        self.chunking_settings = chunking_settings
        self.count_tokens = count_tokens
        self.complete = complete
        self.index_profiles = index_profiles or {}
        self.embeddings = embeddings or {}
        self.content_indexes = content_indexes or {}
        self.record_model_usage = record_model_usage
        self.progress = progress
        self.chunking_model = chunking_model
        self.count_model_tokens = count_model_tokens

    async def _report(self, ctx: ExecutionContext, stage: ProgressStage) -> None:
        if self.progress is not None:
            await self.progress(ctx, None, None)(stage)

    async def execute(self, ctx: ExecutionContext) -> WorkflowSucceeded:
        source = await _settled_work(lambda: self._load_source(ctx))
        entry_stage = source["stage"]
        parsed = None
        if entry_stage in {IngestionStage.REGISTERED, IngestionStage.PARSING}:
            parsed = await self.run_parsing(ctx, source=source)
            extracted = parsed[2] if parsed is not None else None
        else:
            extracted = _saved_effective_date(source)
        chunks = None
        if source["stage"] in {IngestionStage.NORMALIZING, IngestionStage.CHUNKING}:
            normalized = await self.run_normalizing(ctx, parsed, source=source)
            chunks, _ = await self.run_chunking(ctx, normalized, source=source)
        elif entry_stage in {IngestionStage.EMBEDDING, IngestionStage.INDEXING}:
            try:
                chunks, _ = await self._restore_chunk_batch(ctx, source)
            except _BudgetStopped:
                raise asyncio.CancelledError from None
            except asyncio.CancelledError:
                if await _settled_work(lambda: self._check(ctx)):
                    raise ExecutionError(
                        ErrorCode.EXECUTION_INTERRUPTED,
                        "TASK_RETRYABLE",
                        "Artifact recovery stopped; resume from the committed stage",
                    ) from None
                raise
        if source["stage"] != IngestionStage.PUBLISHING:
            await self.run_indexing(
                ctx,
                chunks,
                source=source,
                extracted=extracted,
                restoring=entry_stage == IngestionStage.EMBEDDING,
            )
        await self.run_publishing(ctx, source=source, extracted=extracted)
        return WorkflowSucceeded()

    def _check(self, ctx: ExecutionContext) -> bool:
        with self.repository.session() as session, session.begin():
            return self.check_execution(session, ctx)

    async def _permitted(self, ctx: ExecutionContext) -> bool:
        if not await _settled_work(lambda: self._check(ctx)):
            raise _BudgetStopped
        return True

    def _load_source(self, ctx: ExecutionContext) -> IngestionSource:
        with self.repository.session() as session:
            return self.repository.load_ingestion_source(session, ctx.job_id)

    def _save(
        self,
        ctx: ExecutionContext,
        expected: IngestionStage,
        target: IngestionStage,
        ref: ArtifactRef | None,
        extracted: PolicyDate | None = None,
    ) -> bool:
        with self.repository.session() as session, session.begin():
            if not self.check_execution(session, ctx):
                return False  # Commit the state service's budget termination.
            self.repository.save_parsed_checkpoint(
                session, ctx.job_id, expected, target, ref, extracted
            )
        return True

    async def _artifact_io[T](
        self, operation: Callable[[], T], ctx: ExecutionContext, error_code: str
    ) -> T:
        async def attempt() -> T:
            try:
                return await _settled_work(operation)
            except (OSError, ValueError) as exc:
                temporary = isinstance(exc, OSError) and exc.errno in {
                    errno.EAGAIN,
                    errno.EBUSY,
                    errno.EINTR,
                    errno.ETIMEDOUT,
                }
                category = "LOCAL_RETRYABLE" if temporary else "PERMANENT"
                if isinstance(exc, OSError) and exc.errno == errno.EIO:
                    category = "OUTCOME_UNKNOWN"
                raise ExecutionError(
                    error_code, category, "Stage artifact could not be accessed or restored"
                ) from exc

        return await retry_safe_operation(attempt, lambda: self._permitted(ctx))

    def _load_parsed(
        self, ref: ArtifactRef, document_id: UUID, source_ref: ArtifactRef
    ) -> ParsedDocument:
        with self.store.open_artifact(ref) as source:
            content = source.read()
        if len(content) != ref.size_bytes or hashlib.sha256(content).hexdigest() != ref.sha256:
            raise ValueError("parsed artifact integrity mismatch")
        parsed = ParsedDocument.model_validate_json(content)
        if parsed.document_id != document_id or parsed.source_hash != source_ref.sha256:
            raise ValueError("parsed checkpoint belongs to another original")
        return parsed

    async def run_parsing(
        self, ctx: ExecutionContext, *, source: IngestionSource | None = None
    ) -> tuple[ParsedDocument, ArtifactRef, PolicyDate | None] | None:
        try:
            if source is None:
                source = await _settled_work(lambda: self._load_source(ctx))
            return await self._run_parsing(ctx, source)
        except _BudgetStopped:
            # No second guard or error write: the first transaction settled the task.
            raise asyncio.CancelledError from None
        except asyncio.CancelledError:
            # ParserPort has settled this file's conversion resources; all our IO has settled.
            if await _settled_work(lambda: self._check(ctx)):
                raise ExecutionError(
                    ErrorCode.EXECUTION_INTERRUPTED,
                    "TASK_RETRYABLE",
                    "Parsing stopped; immutable artifacts are safe to resume",
                ) from None
            raise

    async def _run_parsing(
        self, ctx: ExecutionContext, source: IngestionSource
    ) -> tuple[ParsedDocument, ArtifactRef, PolicyDate | None] | None:
        stage = source["stage"]
        if stage not in {IngestionStage.REGISTERED, IngestionStage.PARSING}:
            return None  # The full executor continues normalization without reopening the parse artifact.
        if stage == IngestionStage.REGISTERED:
            if not await _settled_work(
                lambda: self._save(ctx, stage, IngestionStage.PARSING, None)
            ):
                raise _BudgetStopped
            stage = IngestionStage.PARSING
            source["stage"] = stage
        saved = source["checkpoint"].get("parsed_document_ref")
        if saved is not None:
            try:
                ref = ArtifactRef.model_validate(saved)
            except ValidationError as exc:
                raise ExecutionError(
                    ErrorCode.PARSING_ARTIFACT_ERROR,
                    "PERMANENT",
                    "Parsing checkpoint reference is invalid",
                ) from exc
            parsed = await self._artifact_io(
                lambda: self._load_parsed(ref, source["document_id"], source["source_ref"]),
                ctx,
                ErrorCode.PARSING_ARTIFACT_ERROR,
            )
        else:
            # The adapter only offers LOCAL_RETRYABLE before launching conversion,
            # when reading the immutable original failed without side effects.
            async def parse():
                await self._report(ctx, ProgressStage.PARSING)
                return await self.parser.parse(
                    source["document_id"],
                    source["source_ref"],
                    check_execution=lambda: self._permitted(ctx),
                )

            parsed = await retry_safe_operation(
                parse,
                lambda: self._permitted(ctx),
            )
            content = parsed.model_dump_json().encode("utf-8")
            ref = await self._artifact_io(
                lambda: self.store.write_artifact(
                    content, media_type="application/json", artifact_kind="PARSED"
                ),
                ctx,
                ErrorCode.PARSING_ARTIFACT_ERROR,
            )
        failure = decide_parsing_handoff(parsed)
        target = IngestionStage.PARSING if failure else IngestionStage.NORMALIZING
        extracted = (
            _saved_effective_date(source)
            if "extracted_effective_from" in source["checkpoint"]
            else extract_effective_date(parsed)
        )
        if not await _settled_work(lambda: self._save(ctx, stage, target, ref, extracted)):
            raise _BudgetStopped
        source["stage"] = target
        source["checkpoint"]["parsed_document_ref"] = ref.model_dump(mode="json")
        source["checkpoint"]["extracted_effective_from"] = (
            extracted.model_dump(mode="json") if extracted else None
        )
        if failure:
            raise failure
        return parsed, ref, extracted

    async def run_normalizing(
        self,
        ctx: ExecutionContext,
        parsed_result: tuple[ParsedDocument, ArtifactRef, PolicyDate | None] | None = None,
        *,
        source: IngestionSource | None = None,
    ) -> NormalizedDocument:
        """Run at NORMALIZING, or rebuild input at CHUNKING after a restart."""
        try:
            if source is None:
                source = await _settled_work(lambda: self._load_source(ctx))
            await self._permitted(ctx)
            if parsed_result is None:
                try:
                    ref = ArtifactRef.model_validate(
                        source["checkpoint"].get("parsed_document_ref")
                    )
                except ValidationError as exc:
                    raise ExecutionError(
                        ErrorCode.PARSING_ARTIFACT_ERROR,
                        "PERMANENT",
                        "Parsing checkpoint reference is invalid",
                    ) from exc
                parsed = await self._artifact_io(
                    lambda: self._load_parsed(ref, source["document_id"], source["source_ref"]),
                    ctx,
                    ErrorCode.PARSING_ARTIFACT_ERROR,
                )
            else:
                parsed, ref, _ = parsed_result
            await self._report(ctx, ProgressStage.NORMALIZING)
            normalized = await _settled_work(lambda: normalize_document(parsed, ref))
            if source["stage"] == IngestionStage.NORMALIZING:
                if not await _settled_work(
                    lambda: self._save(
                        ctx, IngestionStage.NORMALIZING, IngestionStage.CHUNKING, None
                    )
                ):
                    raise _BudgetStopped
                source["stage"] = IngestionStage.CHUNKING
            else:
                # CHUNKING reconstruction has no progress write, but execution rights can change.
                await self._permitted(ctx)
            return normalized
        except _BudgetStopped:
            raise asyncio.CancelledError from None
        except asyncio.CancelledError:
            # In-flight conversion and IO have settled before checking the real stop reason.
            if await _settled_work(lambda: self._check(ctx)):
                raise ExecutionError(
                    ErrorCode.EXECUTION_INTERRUPTED,
                    "TASK_RETRYABLE",
                    "Normalization stopped; resume from the immutable parsed artifact",
                ) from None
            raise

    def _chunking_generation(self, ctx: ExecutionContext) -> tuple[UUID, str] | None:
        with self.repository.session() as session, session.begin():
            if not self.check_execution(session, ctx):
                return None
            return self.repository.get_or_create_chunking_generation(
                session, ctx.job_id, self.chunking_settings.strategy
            )

    def _mark_chunking_model(self, ctx: ExecutionContext, started: bool) -> bool:
        try:
            with self.repository.session() as session, session.begin():
                if not self.check_execution(session, ctx):
                    return False
                self.repository.mark_chunking_model_started(session, ctx.job_id, started)
            return True
        except SQLAlchemyError as error:
            raise ExecutionError(
                ErrorCode.CHUNKING_MODEL_OUTCOME_UNKNOWN,
                "OUTCOME_UNKNOWN",
                "Chunking model interval commit unconfirmed",
            ) from error

    def _record_chunking_usage(self, ctx: ExecutionContext, input_tokens: int) -> None:
        if self.record_model_usage is None:
            raise ExecutionError(
                ErrorCode.CHUNKING_MODEL_CONFIGURATION_ERROR,
                "PERMANENT",
                "Token usage persistence is not configured",
            )
        try:
            with self.repository.session() as session, session.begin():
                self.record_model_usage(session, ctx.job_id, input_tokens)
        except SQLAlchemyError as error:
            raise ExecutionError(
                ErrorCode.CHUNKING_MODEL_OUTCOME_UNKNOWN,
                "OUTCOME_UNKNOWN",
                "Chunking usage commit unconfirmed",
            ) from error

    def _save_chunked(self, ctx: ExecutionContext, ref: ArtifactRef) -> bool:
        with self.repository.session() as session, session.begin():
            if not self.check_execution(session, ctx):
                return False
            self.repository.save_chunked_checkpoint(session, ctx.job_id, ref)
        return True

    def _load_chunk_batch(
        self, ref: ArtifactRef, document_id: UUID, source_hash: str, generation_id: UUID
    ) -> ChunkBatch:
        with self.store.open_artifact(ref) as source:
            content = source.read()
        if len(content) != ref.size_bytes or hashlib.sha256(content).hexdigest() != ref.sha256:
            raise ValueError("chunk artifact integrity mismatch")
        batch = ChunkBatch.model_validate_json(content)
        for ordinal, chunk in enumerate(batch.chunks):
            if (chunk.document_id, chunk.source_hash, chunk.generation_id, chunk.ordinal) != (
                document_id,
                source_hash,
                generation_id,
                ordinal,
            ):
                raise ValueError("chunk checkpoint identity or order mismatch")
            if chunk.chunk_id != str(uuid5(generation_id, str(ordinal))):
                raise ValueError("chunk checkpoint identifier mismatch")
            if any(
                (loc.document_id, loc.source_hash) != (document_id, source_hash)
                for loc in chunk.locators
            ):
                raise ValueError("chunk locator belongs to another original")
        return batch

    async def _restore_chunk_batch(
        self, ctx: ExecutionContext, source: IngestionSource
    ) -> tuple[ChunkBatch, ArtifactRef]:
        try:
            ref = ArtifactRef.model_validate(source["checkpoint"]["chunk_batch_ref"])
            generation_id = UUID(str(source["checkpoint"]["generation_id"]))
        except (ValidationError, ValueError) as exc:
            raise ExecutionError(
                ErrorCode.CHUNKING_ARTIFACT_ERROR,
                "PERMANENT",
                "Chunking checkpoint reference is invalid",
            ) from exc
        batch = await self._artifact_io(
            lambda: self._load_chunk_batch(
                ref, source["document_id"], source["source_ref"].sha256, generation_id
            ),
            ctx,
            ErrorCode.CHUNKING_ARTIFACT_ERROR,
        )
        return batch, ref

    async def run_chunking(
        self,
        ctx: ExecutionContext,
        normalized: NormalizedDocument | None = None,
        *,
        source: IngestionSource | None = None,
    ) -> tuple[ChunkBatch, ArtifactRef]:
        try:
            if source is None:
                source = await _settled_work(lambda: self._load_source(ctx))
            await self._permitted(ctx)
            if source["stage"] == IngestionStage.EMBEDDING:
                return await self._restore_chunk_batch(ctx, source)
            if source["checkpoint"].get("chunking_model_started"):
                raise ExecutionError(
                    ErrorCode.CHUNKING_MODEL_OUTCOME_UNKNOWN,
                    "OUTCOME_UNKNOWN",
                    "Model call has no committed chunk artifact",
                )
            saved_generation = source["checkpoint"].get("generation_id")
            strategy = source["checkpoint"].get("chunking_strategy")
            if saved_generation is None or strategy is None:
                binding = await _settled_work(lambda: self._chunking_generation(ctx))
                if binding is None:
                    raise _BudgetStopped
                generation_id, strategy = binding
            else:
                generation_id = UUID(str(saved_generation))
            source["checkpoint"]["generation_id"] = str(generation_id)
            source["checkpoint"]["chunking_strategy"] = strategy
            if normalized is None:
                normalized = await self.run_normalizing(ctx, source=source)
            await self._report(ctx, ProgressStage.CHUNKING)
            if strategy == "model":
                if (
                    self.chunking_model is None
                    or self.count_model_tokens is None
                    or self.record_model_usage is None
                ):
                    raise ExecutionError(
                        ErrorCode.CHUNKING_MODEL_CONFIGURATION_ERROR,
                        "PERMANENT",
                        "Bound model strategy resources are not configured",
                    )

                async def mark(started: bool) -> None:
                    if not await _settled_work(lambda: self._mark_chunking_model(ctx, started)):
                        raise _BudgetStopped

                batch = await run_model_chunking(
                    normalized,
                    document_id=source["document_id"],
                    source_hash=source["source_ref"].sha256,
                    generation_id=generation_id,
                    settings=self.chunking_settings,
                    count_tokens=self.count_tokens,
                    count_model_tokens=self.count_model_tokens,
                    gateway=self.chunking_model,
                    check_execution=lambda: self._permitted(ctx),
                    mark_started=mark,
                    record_usage=lambda tokens: _settled_work(
                        lambda: self._record_chunking_usage(ctx, tokens)
                    ),
                )
            else:
                batch = await _settled_work(
                    lambda: chunk_document(
                        normalized,
                        document_id=source["document_id"],
                        source_hash=source["source_ref"].sha256,
                        generation_id=generation_id,
                        settings=self.chunking_settings,
                        count_tokens=self.count_tokens,
                    )
                )
            # Serialization is CPU work, outside the IO retry/error boundary.
            content = await _settled_work(lambda: batch.model_dump_json().encode("utf-8"))
            ref = await self._artifact_io(
                lambda: self.store.write_artifact(
                    content, media_type="application/json", artifact_kind="CHUNKED"
                ),
                ctx,
                ErrorCode.CHUNKING_ARTIFACT_ERROR,
            )
            if not await _settled_work(lambda: self._save_chunked(ctx, ref)):
                raise _BudgetStopped
            source["stage"] = IngestionStage.EMBEDDING
            source["checkpoint"]["chunk_batch_ref"] = ref.model_dump(mode="json")
            return batch, ref
        except _BudgetStopped:
            raise asyncio.CancelledError from None
        except asyncio.CancelledError:
            if source is not None and source["checkpoint"].get("chunking_strategy") == "model":
                # Cancellation can arrive while either the interval or artifact commit settles.
                try:
                    committed = await _settled_work(lambda: self._load_source(ctx))
                except SQLAlchemyError as error:
                    raise ExecutionError(
                        ErrorCode.CHUNKING_MODEL_OUTCOME_UNKNOWN,
                        "OUTCOME_UNKNOWN",
                        "Chunk checkpoint commit unconfirmed",
                    ) from error
                if (
                    committed["checkpoint"].get("chunking_model_started")
                    and committed["checkpoint"].get("chunk_batch_ref") is None
                ):
                    raise ExecutionError(
                        ErrorCode.CHUNKING_MODEL_OUTCOME_UNKNOWN,
                        "OUTCOME_UNKNOWN",
                        "Model call has no committed chunk artifact",
                    ) from None
            if await _settled_work(lambda: self._check(ctx)):
                raise ExecutionError(
                    ErrorCode.EXECUTION_INTERRUPTED,
                    "TASK_RETRYABLE",
                    "Chunking stopped; resume from immutable stage artifacts",
                ) from None
            raise

    def _prepare_embedding(
        self,
        ctx: ExecutionContext,
        source: IngestionSource,
        extracted: PolicyDate | None,
    ) -> tuple[str, IndexFilterMetadata] | None:
        with self.repository.session() as session, session.begin():
            if not self.check_execution(session, ctx):
                return None
            bound = source["checkpoint"].get("index_profile_id")
            profile_id = (
                str(bound)
                if bound is not None
                else self.repository.get_or_bind_index_profile(session, ctx.job_id)
            )
            logical, current = self.repository.load_indexing_source_metadata(session, ctx.job_id)
            try:
                resolved = resolve_policy_metadata(current, extracted)
            except ValueError as error:
                raise ExecutionError(
                    ErrorCode.INVALID_REGISTRATION, "PERMANENT", "Effective date range is invalid"
                ) from error
            metadata = project_index_filter_metadata(logical, resolved)
        source["checkpoint"]["index_profile_id"] = profile_id
        return profile_id, metadata

    async def run_embedding(
        self,
        ctx: ExecutionContext,
        chunks: ChunkBatch,
        profile_id: str,
        metadata: IndexFilterMetadata,
        *,
        restoring: bool,
    ) -> AsyncGenerator[IndexRecordBatch]:
        """Yield one batch at a time; the indexing consumer owns persistence and progress."""
        try:
            profile = self.index_profiles[profile_id]
            embedding = self.embeddings[profile_id]
            index = self.content_indexes[profile_id]
        except KeyError as exc:
            raise ExecutionError(
                ErrorCode.INDEX_PROFILE_UNAVAILABLE,
                "PERMANENT",
                "Bound index profile is not loaded",
            ) from exc
        record_usage = self.record_model_usage
        if record_usage is None:
            raise ExecutionError(
                ErrorCode.INDEX_PROFILE_UNAVAILABLE,
                "PERMANENT",
                "Token usage persistence is not configured",
            )
        for offset in range(0, len(chunks.chunks), profile.embedding.batch_size):
            window = chunks.chunks[offset : offset + profile.embedding.batch_size]
            if restoring:
                existing = await retry_safe_operation(
                    lambda window=window: _settled_work(
                        lambda: index.existing_record_ids([chunk.chunk_id for chunk in window])
                    ),
                    lambda: self._permitted(ctx),
                )
                window = [chunk for chunk in window if chunk.chunk_id not in existing]
            if not window:
                continue
            vectors = await retry_safe_operation(
                lambda window=window: self._embed_and_record(
                    ctx, embedding, [chunk.retrieval_text for chunk in window], record_usage
                ),
                lambda: self._permitted(ctx),
            )
            # Execution rights may have changed while waiting on the external service.
            await self._permitted(ctx)
            yield assemble_index_records(window, vectors, metadata)
            del vectors

    def _advance_stage(
        self, ctx: ExecutionContext, expected: IngestionStage, target: IngestionStage
    ) -> bool:
        with self.repository.session() as session, session.begin():
            if not self.check_execution(session, ctx):
                return False  # Preserve a budget termination committed by the guard.
            self.repository.advance_ingestion_stage(session, ctx.job_id, expected, target)
        return True

    async def run_indexing(
        self,
        ctx: ExecutionContext,
        chunks: ChunkBatch | None = None,
        *,
        source: IngestionSource | None = None,
        extracted: PolicyDate | None = None,
        restoring: bool,
    ) -> None:
        """Persist embedding batches and hand queryable records to publication, without publishing."""
        try:
            if source is None:
                source = await _settled_work(lambda: self._load_source(ctx))
                extracted = _saved_effective_date(source)
            await self._permitted(ctx)
            stage = source["stage"]
            if stage == IngestionStage.PUBLISHING:
                return
            if chunks is None:
                chunks, _ = await self._restore_chunk_batch(ctx, source)
            generation_id = UUID(str(source["checkpoint"]["generation_id"]))
            expected_count = len(chunks.chunks)
            if stage == IngestionStage.EMBEDDING:
                prepared = await _settled_work(
                    lambda: self._prepare_embedding(ctx, source, extracted)
                )
                if prepared is None:
                    raise _BudgetStopped
                profile_id, metadata = prepared
            else:
                # INDEXING recovery needs only the original physical index binding.
                profile_id = str(source["checkpoint"]["index_profile_id"])
            try:
                index = self.content_indexes[profile_id]
            except KeyError as exc:
                raise ExecutionError(
                    ErrorCode.INDEX_PROFILE_UNAVAILABLE,
                    "PERMANENT",
                    "Bound index profile is not loaded",
                ) from exc
            if stage == IngestionStage.EMBEDDING:
                async with aclosing(
                    self.run_embedding(ctx, chunks, profile_id, metadata, restoring=restoring)
                ) as batches:
                    async for records in batches:

                        async def write(records=records):
                            await self._report(ctx, ProgressStage.INDEXING)
                            return await _settled_work(lambda: index.write_records(records))

                        await retry_safe_operation(
                            write,
                            lambda: self._permitted(ctx),
                        )
                        del records
                if not await _settled_work(
                    lambda: self._advance_stage(
                        ctx, IngestionStage.EMBEDDING, IngestionStage.INDEXING
                    )
                ):
                    raise _BudgetStopped
                source["stage"] = IngestionStage.INDEXING
            if stage == IngestionStage.INDEXING:
                await self._report(ctx, ProgressStage.INDEXING)
            await retry_safe_operation(
                lambda: _settled_work(
                    lambda: index.confirm_queryable(generation_id, expected_count)
                ),
                lambda: self._permitted(ctx),
            )
            if not await _settled_work(
                lambda: self._advance_stage(ctx, IngestionStage.INDEXING, IngestionStage.PUBLISHING)
            ):
                raise _BudgetStopped
            source["stage"] = IngestionStage.PUBLISHING
        except _BudgetStopped:
            raise asyncio.CancelledError from None
        except asyncio.CancelledError:
            if await _settled_work(lambda: self._check(ctx)):
                raise ExecutionError(
                    ErrorCode.EXECUTION_INTERRUPTED,
                    "TASK_RETRYABLE",
                    "Indexing stopped; resume using indexed records and the committed stage",
                ) from None
            raise

    async def run_publishing(
        self,
        ctx: ExecutionContext,
        *,
        source: IngestionSource | None = None,
        extracted: PolicyDate | None = None,
    ) -> None:
        try:
            if source is None:
                source = await _settled_work(lambda: self._load_source(ctx))
                extracted = _saved_effective_date(source)
            identity = SourceReference(
                document_id=source["document_id"], source_hash=source["source_ref"].sha256
            )
            checkpoint = source["checkpoint"]
            metadata_task = "metadata_update" in checkpoint
            target = (
                checkpoint.get("target_generation_id")
                if metadata_task
                else checkpoint["generation_id"]
            )
            generation_id = UUID(str(target)) if target is not None else None
            profile_id = str(checkpoint["index_profile_id"]) if generation_id else None
            raw_update = checkpoint.get("metadata_update")
            update = (
                SourceMetadataUpdate.model_validate({"new_job_id": ctx.job_id, **raw_update})
                if isinstance(raw_update, dict)
                else None
            )
            if (
                update is not None
                and "effective_from" in update.model_fields_set
                and update.effective_from is None
            ):

                def read_parsed_ref():
                    with self.repository.session() as read_session:
                        return self.repository.load_source_parsed_ref(
                            read_session, identity, generation_id
                        )

                parsed_ref = await _settled_work(read_parsed_ref)
                if parsed_ref is not None:
                    parsed = await self._artifact_io(
                        lambda: self._load_parsed(
                            parsed_ref, source["document_id"], source["source_ref"]
                        ),
                        ctx,
                        ErrorCode.PARSING_ARTIFACT_ERROR,
                    )
                    extracted = extract_effective_date(parsed)
            while True:
                await self._permitted(ctx)
                async with AsyncExitStack() as resources:
                    scope = self.repository.publication_scope(identity.document_id)

                    def enter_scope(scope=scope) -> Session | None:
                        session = scope.__enter__()
                        # Register before returning: cancellation while acquiring still releases it.
                        resources.push_async_callback(
                            _settled_work, partial(scope.__exit__, None, None, None)
                        )
                        return session

                    session = await _settled_work(enter_scope)
                    if session is not None:
                        await self._report(ctx, ProgressStage.PUBLISHING)
                        await self._publish_locked(
                            ctx, session, identity, generation_id, profile_id, extracted, update
                        )
                        return
                await asyncio.sleep(0.1)
        except _BudgetStopped:
            raise asyncio.CancelledError from None
        except asyncio.CancelledError:
            if await _settled_work(lambda: self._check(ctx)):
                raise ExecutionError(
                    ErrorCode.EXECUTION_INTERRUPTED,
                    "TASK_RETRYABLE",
                    "Publication stopped; resume from the committed stage",
                ) from None
            raise

    async def _publish_locked(
        self,
        ctx: ExecutionContext,
        session: Session,
        identity: SourceReference,
        generation_id: UUID | None,
        profile_id: str | None,
        extracted: PolicyDate | None,
        update: SourceMetadataUpdate | None,
    ) -> None:
        def read():
            with session.begin():
                return self.repository.load_publication_sources(session, identity)

        logical, policy, published = await _settled_work(read)
        try:
            resolved = resolve_policy_metadata(policy, extracted, update)
        except ValueError as error:
            raise ExecutionError(
                ErrorCode.INVALID_REGISTRATION, "PERMANENT", "Effective date range is invalid"
            ) from error
        first_publication = not any(entry.source == identity for entry in published)
        has_content = generation_id is not None or not first_publication
        promote = (update is not None and update.version_status == VersionStatus.CURRENT) or (
            update is None
            and first_publication
            and resolved.version_status == VersionStatus.CURRENT
        )
        updates = compute_version_updates(
            published if has_content else [],
            identity,
            resolved,
            promote if has_content else False,
        )
        targets = []
        if generation_id is not None:
            targets.append(
                (
                    generation_id,
                    profile_id,
                    project_index_filter_metadata(logical, updates[identity]),
                )
            )
        for entry in published:
            changed = updates.get(entry.source)
            if changed is not None and (
                entry.source != identity or entry.generation_id != generation_id
            ):
                targets.append(
                    (
                        entry.generation_id,
                        entry.index_profile_id,
                        project_index_filter_metadata(logical, changed),
                    )
                )
        for target_generation, target_profile, metadata in targets:
            try:
                index = self.content_indexes[target_profile]
            except KeyError as exc:
                raise ExecutionError(
                    ErrorCode.INDEX_PROFILE_UNAVAILABLE,
                    "PERMANENT",
                    "Bound index profile is not loaded",
                ) from exc
            await retry_safe_operation(
                partial(
                    _settled_work, partial(index.sync_filter_metadata, target_generation, metadata)
                ),
                lambda: self._permitted(ctx),
            )

        def commit() -> bool:
            with session.begin():
                if not self.check_execution(session, ctx):
                    return False
                for changed_source, metadata in updates.items():
                    self.repository.update_policy_metadata(session, changed_source, metadata)
                if has_content:
                    if update is None and generation_id is not None:
                        self.repository.publish_source(session, identity, generation_id)
                    if update is None or any(
                        metadata
                        != next((e.policy_metadata for e in published if e.source == key), None)
                        for key, metadata in updates.items()
                    ):
                        self.repository.increment_knowledge_revision(session, logical.kb_id)
                self.complete(session, ctx)
            return True

        if not await _settled_work(commit):
            raise _BudgetStopped

    async def _embed_and_record(
        self,
        ctx: ExecutionContext,
        embedding: EmbeddingPort,
        texts: list[str],
        record_usage: Callable[[Session, UUID, int], None],
    ) -> list[list[float]]:
        async def request() -> list[list[float]]:
            await self._report(ctx, ProgressStage.EMBEDDING)
            vectors, usage = await embedding.embed_documents(texts)
            input_tokens = usage.input_tokens
            if input_tokens is None:
                raise ExecutionError(
                    ErrorCode.EMBEDDING_INVALID_RESPONSE,
                    "OUTCOME_UNKNOWN",
                    "PPIO did not return token usage; stop for usage reconciliation",
                )
            # No retry wrapper: an unknown PG commit must not count a response twice.
            await _settled_work(
                lambda: self._record_embedding_usage(ctx, input_tokens, record_usage)
            )
            return vectors

        task = asyncio.create_task(request())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            # Persist the returned usage before exposing normal shutdown/cancellation.
            await task
            raise

    def _record_embedding_usage(
        self,
        ctx: ExecutionContext,
        input_tokens: int,
        record_usage: Callable[[Session, UUID, int], None],
    ) -> None:
        with self.repository.session() as session, session.begin():
            record_usage(session, ctx.job_id, input_tokens)
