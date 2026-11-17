"""Agent Worker and shared question-answering resources, loaded once per process."""

import asyncio
import logging
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from functools import partial
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx
from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import SecretStr
from sqlalchemy import Engine

from intelligent_reimbursement_assitant.content_index.adapters.elasticsearch import (
    ElasticsearchContentIndexAdapter,
)
from intelligent_reimbursement_assitant.document_ingestion_service.adapters.repository import (
    SQLAlchemyDocumentRepository,
)
from intelligent_reimbursement_assitant.embedding.adapters.bge_api import BgeM3ApiAdapter
from intelligent_reimbursement_assitant.entrypoints.checkpoints import checkpoint_conninfo
from intelligent_reimbursement_assitant.entrypoints.configuration import (
    AppSettings,
    ConfigurationError,
    load_settings,
    queue_binding,
)
from intelligent_reimbursement_assitant.execution_state_service.adapters.events import (
    SQLAlchemyEventRepository,
)
from intelligent_reimbursement_assitant.execution_state_service.application.events import (
    bind_progress,
)
from intelligent_reimbursement_assitant.execution_state_service.application.service import (
    ExecutionStateService,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import ExecutionKind
from intelligent_reimbursement_assitant.knowledge_wiki.adapters.reading import LocalWikiReader
from intelligent_reimbursement_assitant.knowledge_wiki.adapters.repository import (
    SQLAlchemyWikiRepository,
)
from intelligent_reimbursement_assitant.langgraph_executor.adapters.checkpoints import (
    FencedPostgresSaver,
    PostgresQARecoveryReader,
)
from intelligent_reimbursement_assitant.langgraph_executor.application.executor import (
    LangGraphExecutor,
)
from intelligent_reimbursement_assitant.model_gateway.adapters.tokenizer import (
    load_token_counter,
)
from intelligent_reimbursement_assitant.model_gateway.domain.config import ModelSettings
from intelligent_reimbursement_assitant.policy_qa_graph.application.answering import (
    generate_answer,
)
from intelligent_reimbursement_assitant.policy_qa_graph.application.evidence import (
    assess_evidence,
    prepare_evidence_clarification,
)
from intelligent_reimbursement_assitant.policy_qa_graph.application.graph import QANodes
from intelligent_reimbursement_assitant.policy_qa_graph.application.retrieval import (
    execute_tool,
    refresh_item_ranking,
)
from intelligent_reimbursement_assitant.policy_qa_graph.application.tool_calling import (
    coordinate_tools,
)
from intelligent_reimbursement_assitant.policy_qa_graph.application.understanding import (
    apply_clarification,
    understand,
)
from intelligent_reimbursement_assitant.policy_qa_service.adapters.bge_reranker import (
    PPIOBgeReranker,
)
from intelligent_reimbursement_assitant.policy_qa_service.adapters.deepseek import (
    DeepSeekModelGateway,
)
from intelligent_reimbursement_assitant.policy_qa_service.adapters.repository import (
    SQLAlchemyQARepository,
)
from intelligent_reimbursement_assitant.policy_qa_service.adapters.reranker_tokenizer import (
    load_pair_token_counter,
)
from intelligent_reimbursement_assitant.policy_qa_service.application.evidence import (
    ModelEvidenceJudge,
)
from intelligent_reimbursement_assitant.policy_qa_service.application.service import PolicyQAService
from intelligent_reimbursement_assitant.policy_qa_service.domain.config import (
    QASettings,
    RerankerSettings,
)
from intelligent_reimbursement_assitant.retriever.application.retrieval import Retriever
from intelligent_reimbursement_assitant.scanner.adapters.local_artifact_store import (
    LocalArtifactStore,
)
from intelligent_reimbursement_assitant.worker_runtime.entrypoints.worker import serve_worker


@asynccontextmanager
async def open_qa_nodes(
    settings: AppSettings, engine: Engine, state_service: ExecutionStateService
) -> AsyncGenerator[tuple[PolicyQAService, QANodes]]:
    qa = cast(QASettings, settings.qa)
    model = cast(ModelSettings, qa.model)
    reranker_settings = cast(RerankerSettings, qa.reranker)
    failed = False
    try:
        counter = load_token_counter(qa.tokenizer_dir)
    except Exception:
        failed = True
    if failed:
        raise ConfigurationError("qa", [("qa.tokenizer_dir", "tokenizer_load")])
    try:
        pair_counter = load_pair_token_counter(reranker_settings.tokenizer_dir)
    except Exception:
        failed = True
    if failed:
        raise ConfigurationError("qa", [("qa.reranker.tokenizer_dir", "tokenizer_load")])
    io_pool = asyncio.Semaphore(qa.tool_concurrency)
    repository = SQLAlchemyQARepository(engine)
    wiki_reader = LocalWikiReader(
        LocalArtifactStore(cast(Path, settings.managed_directory) / "quarantine")
    )
    progress = partial(bind_progress, SQLAlchemyEventRepository(engine), state_service)
    service = PolicyQAService(
        repository,
        SQLAlchemyDocumentRepository(engine),
        state_service,
        qa,
        SQLAlchemyWikiRepository(engine),
    )
    async with AsyncExitStack() as resources:
        client = await resources.enter_async_context(httpx.AsyncClient(follow_redirects=False))
        embeddings, indexes = {}, {}
        for profile_id, profile in sorted(settings.index_profiles.items()):
            embedding = BgeM3ApiAdapter(profile.embedding)
            resources.push_async_callback(embedding.aclose)
            embeddings[profile_id] = embedding
            index = ElasticsearchContentIndexAdapter(profile.elasticsearch)
            resources.push_async_callback(asyncio.to_thread, index.close)
            indexes[profile_id] = index
        key = cast(
            SecretStr,
            settings.index_profiles[reranker_settings.api_key_profile_id].embedding.api_key,
        )
        reranker = PPIOBgeReranker(
            reranker_settings, key.get_secret_value(), pair_counter, io_pool=io_pool
        )
        resources.push_async_callback(reranker.aclose)
        ranking = partial(
            refresh_item_ranking,
            settings=qa,
            reranker=reranker,
            repository=repository,
            state_service=state_service,
        )
        tool_executor = partial(
            execute_tool,
            retriever=Retriever(indexes, io_pool=io_pool),
            io_pool=io_pool,
            wiki_reader=wiki_reader,
            embeddings=embeddings,
            settings=qa,
            repository=repository,
            state_service=state_service,
        )
        gateway = DeepSeekModelGateway(client, model)
        timezone = ZoneInfo(cast(str, qa.business_timezone))
        judge = ModelEvidenceJudge(
            gateway=gateway,
            repository=repository,
            state_service=state_service,
            settings=qa,
            model_settings=model,
            count_tokens=counter,
            progress=progress,
        )
        yield (
            service,
            QANodes(
                partial(
                    understand,
                    wiki_reader=wiki_reader,
                    io_pool=io_pool,
                    progress=progress,
                    service=service,
                    gateway=gateway,
                    state_service=state_service,
                    settings=qa,
                    model_settings=model,
                    business_timezone=timezone,
                    count_tokens=counter,
                ),
                partial(
                    apply_clarification,
                    progress=progress,
                    service=service,
                    gateway=gateway,
                    state_service=state_service,
                    settings=qa,
                    model_settings=model,
                    business_timezone=timezone,
                    count_tokens=counter,
                ),
                partial(
                    coordinate_tools,
                    progress=progress,
                    repository=repository,
                    gateway=gateway,
                    state_service=state_service,
                    settings=qa,
                    model_settings=model,
                    count_tokens=counter,
                    execute_tool=tool_executor,
                    wiki_reader=wiki_reader,
                    io_pool=io_pool,
                    refresh_item_ranking=ranking,
                    neighbor_radius=qa.neighbor_radius,
                ),
                partial(assess_evidence, judge=judge),
                partial(
                    prepare_evidence_clarification,
                    progress=progress,
                    service=service,
                    gateway=gateway,
                    state_service=state_service,
                    settings=qa,
                    model_settings=model,
                    count_tokens=counter,
                ),
                partial(
                    generate_answer,
                    progress=progress,
                    repository=repository,
                    gateway=gateway,
                    state_service=state_service,
                    settings=qa,
                    model_settings=model,
                    count_tokens=counter,
                ),
            ),
        )


@asynccontextmanager
async def qa_executor_factory(
    settings: AppSettings, engine: Engine, state: ExecutionStateService
) -> AsyncGenerator[LangGraphExecutor]:
    async with open_qa_nodes(settings, engine, state) as (service, nodes):
        async with AsyncConnectionPool[AsyncConnection[dict[str, Any]]](
            checkpoint_conninfo(settings.database_url.get_secret_value()),
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
            min_size=1,
            max_size=2 * service.settings.worker.concurrency + 1,
            open=False,
        ) as pool:
            yield LangGraphExecutor(
                service,
                nodes,
                PostgresQARecoveryReader(engine),
                lambda ctx: FencedPostgresSaver(pool, ctx),
                progress=partial(bind_progress, SQLAlchemyEventRepository(engine), state),
            )


async def _serve(settings: AppSettings) -> None:
    qa = cast(QASettings, settings.qa)
    await serve_worker(
        settings.database_url.get_secret_value(),
        cast(SecretStr, settings.redis_url).get_secret_value(),
        cast(Path, settings.managed_directory),
        partial(qa_executor_factory, settings),
        qa.worker,
        binding=queue_binding(settings.queue_groups, ExecutionKind.AGENT_TURN),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_serve(load_settings("qa")))


if __name__ == "__main__":
    main()
