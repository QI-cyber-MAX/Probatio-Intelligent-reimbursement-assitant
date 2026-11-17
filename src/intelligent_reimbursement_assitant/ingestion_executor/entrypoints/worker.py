"""Own the parser, embedding and index clients for the complete ingestion Worker."""

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import cast

import httpx
from sqlalchemy import Engine

from intelligent_reimbursement_assitant.content_index.adapters.elasticsearch import (
    ElasticsearchContentIndexAdapter,
)
from intelligent_reimbursement_assitant.content_index.domain.config import IndexProfileSettings
from intelligent_reimbursement_assitant.embedding.adapters.bge_api import BgeM3ApiAdapter
from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.execution_state_service.application.service import (
    ExecutionStateService,
)
from intelligent_reimbursement_assitant.ingestion_executor.adapters.tokenizer import (
    load_token_counter,
)
from intelligent_reimbursement_assitant.ingestion_executor.application.parsing import (
    IngestionExecutor,
)
from intelligent_reimbursement_assitant.ingestion_executor.domain.config import IngestionSettings
from intelligent_reimbursement_assitant.ingestion_executor.entrypoints.parsing import (
    build_parsing_stage,
)
from intelligent_reimbursement_assitant.model_gateway.adapters.deepseek import (
    DeepSeekModelGateway,
    ModelErrorCodes,
)
from intelligent_reimbursement_assitant.model_gateway.domain.config import ModelSettings
from intelligent_reimbursement_assitant.parser.adapters.docling.adapter import DoclingParserAdapter
from intelligent_reimbursement_assitant.parser.adapters.mineru.adapter import MinerUParserAdapter
from intelligent_reimbursement_assitant.parser.adapters.mineru.client import MinerUClient
from intelligent_reimbursement_assitant.parser.adapters.routing import RoutingParserAdapter
from intelligent_reimbursement_assitant.parser.domain.config import MinerUSettings
from intelligent_reimbursement_assitant.scanner.adapters.local_artifact_store import (
    LocalArtifactStore,
)


@asynccontextmanager
async def ingestion_stage_factory(
    engine: Engine,
    state: ExecutionStateService,
    settings: IngestionSettings,
    managed_directory: Path,
    index_profiles: Mapping[str, IndexProfileSettings],
) -> AsyncIterator[IngestionExecutor]:
    """Own clients until all stage invocations have settled, including partial startup."""
    for profile in index_profiles.values():
        if (
            profile.embedding.batch_size
            * settings.chunking.max_tokens
            * profile.embedding.requests_per_minute
            > 50_000_000
        ):
            raise ValueError("Reduce batch size or request rate to fit PPIO T2's 50M TPM")
    async with AsyncExitStack() as resources:
        embeddings = {}
        indexes = {}
        for profile_id, profile in index_profiles.items():
            embedding = BgeM3ApiAdapter(profile.embedding)
            resources.push_async_callback(embedding.aclose)
            embeddings[profile_id] = embedding
            index = ElasticsearchContentIndexAdapter(profile.elasticsearch)
            resources.push_async_callback(asyncio.to_thread, index.close)
            indexes[profile_id] = index
        store = LocalArtifactStore(managed_directory / "quarantine")
        office = DoclingParserAdapter(store, settings.parser)
        resources.push_async_callback(office.aclose)
        cloud = None
        if settings.parser.pdf_backend == "mineru":
            api = await resources.enter_async_context(httpx.AsyncClient())
            objects = await resources.enter_async_context(httpx.AsyncClient(follow_redirects=True))
            cloud = MinerUParserAdapter(
                store,
                settings.parser,
                MinerUClient(api, objects, cast(MinerUSettings, settings.parser.mineru)),
            )
        parser = RoutingParserAdapter(office, cloud)
        model = None
        count_model_tokens = None
        if settings.chunking.strategy == "model":
            model_client = await resources.enter_async_context(httpx.AsyncClient())
            model = DeepSeekModelGateway(
                model_client,
                cast(ModelSettings, settings.chunking.model),
                ModelErrorCodes(
                    configuration=ErrorCode.CHUNKING_MODEL_CONFIGURATION_ERROR,
                    unavailable=ErrorCode.CHUNKING_MODEL_UNAVAILABLE,
                    outcome_unknown=ErrorCode.CHUNKING_MODEL_OUTCOME_UNKNOWN,
                ),
            )
            count_model_tokens = load_token_counter(
                cast(Path, settings.chunking.model_tokenizer_dir)
            )

        yield build_parsing_stage(
            engine,
            state,
            store,
            parser,
            settings.chunking,
            index_profiles=index_profiles,
            embeddings=embeddings,
            content_indexes=indexes,
            chunking_model=model,
            count_model_tokens=count_model_tokens,
        )
