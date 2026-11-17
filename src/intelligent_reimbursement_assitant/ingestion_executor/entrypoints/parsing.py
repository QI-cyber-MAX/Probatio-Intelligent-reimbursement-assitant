"""Compose ingestion stages for a full executor or isolated stage invocations."""

from collections.abc import Callable, Mapping
from functools import partial

from sqlalchemy import Engine

from intelligent_reimbursement_assitant.content_index.domain.config import IndexProfileSettings
from intelligent_reimbursement_assitant.content_index.ports.content_index import ContentIndexPort
from intelligent_reimbursement_assitant.document_ingestion_service.adapters.repository import (
    SQLAlchemyDocumentRepository,
)
from intelligent_reimbursement_assitant.embedding.ports.embedding import EmbeddingPort
from intelligent_reimbursement_assitant.execution_state_service.adapters.events import (
    SQLAlchemyEventRepository,
)
from intelligent_reimbursement_assitant.execution_state_service.application.events import (
    bind_progress,
)
from intelligent_reimbursement_assitant.execution_state_service.application.service import (
    ExecutionStateService,
)
from intelligent_reimbursement_assitant.ingestion_executor.adapters.tokenizer import (
    load_token_counter,
)
from intelligent_reimbursement_assitant.ingestion_executor.application.parsing import (
    IngestionExecutor,
)
from intelligent_reimbursement_assitant.ingestion_executor.domain.config import ChunkingSettings
from intelligent_reimbursement_assitant.model_gateway.ports.model_gateway import ModelGateway
from intelligent_reimbursement_assitant.parser.ports.parser import ParserPort
from intelligent_reimbursement_assitant.scanner.ports.artifact_store import ArtifactStorePort


def build_parsing_stage(
    engine: Engine,
    state: ExecutionStateService,
    store: ArtifactStorePort,
    parser: ParserPort,
    chunking_settings: ChunkingSettings,
    *,
    index_profiles: Mapping[str, IndexProfileSettings] | None = None,
    embeddings: Mapping[str, EmbeddingPort] | None = None,
    content_indexes: Mapping[str, ContentIndexPort] | None = None,
    chunking_model: ModelGateway | None = None,
    count_model_tokens: Callable[[str], int] | None = None,
) -> IngestionExecutor:
    return IngestionExecutor(
        SQLAlchemyDocumentRepository(engine),
        store,
        parser,
        state.check_execution,
        chunking_settings,
        load_token_counter(chunking_settings.tokenizer_dir),
        complete=state.complete,
        index_profiles=index_profiles,
        embeddings=embeddings,
        content_indexes=content_indexes,
        record_model_usage=state.record_model_usage,
        progress=partial(bind_progress, SQLAlchemyEventRepository(engine), state),
        chunking_model=chunking_model,
        count_model_tokens=count_model_tokens,
    )
