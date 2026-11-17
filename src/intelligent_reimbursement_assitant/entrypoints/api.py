"""API composition with explicit configuration and owned database lifetime."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import cast

from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.exc import ArgumentError

from intelligent_reimbursement_assitant.document_ingestion_service.adapters.repository import (
    SQLAlchemyDocumentRepository,
)
from intelligent_reimbursement_assitant.document_ingestion_service.application.service import (
    DocumentIngestionService,
)
from intelligent_reimbursement_assitant.document_ingestion_service.domain.config import ApiSettings
from intelligent_reimbursement_assitant.document_ingestion_service.entrypoints.http import (
    install_document_ingestion,
)
from intelligent_reimbursement_assitant.entrypoints.configuration import (
    load_settings,
)
from intelligent_reimbursement_assitant.execution_state_service.adapters.events import (
    SQLAlchemyEventRepository,
)
from intelligent_reimbursement_assitant.execution_state_service.application.service import (
    ExecutionStateService,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionSettings,
)
from intelligent_reimbursement_assitant.knowledge_wiki.adapters.artifacts import WikiArtifactReader
from intelligent_reimbursement_assitant.knowledge_wiki.adapters.repository import (
    SQLAlchemyWikiRepository,
)
from intelligent_reimbursement_assitant.knowledge_wiki.application.service import WikiService
from intelligent_reimbursement_assitant.knowledge_wiki.domain.config import WikiSettings
from intelligent_reimbursement_assitant.knowledge_wiki.entrypoints.http import install_wiki
from intelligent_reimbursement_assitant.policy_qa_service.adapters.repository import (
    SQLAlchemyQARepository,
)
from intelligent_reimbursement_assitant.policy_qa_service.application.service import PolicyQAService
from intelligent_reimbursement_assitant.policy_qa_service.domain.config import QASettings
from intelligent_reimbursement_assitant.policy_qa_service.entrypoints.http import install_policy_qa
from intelligent_reimbursement_assitant.scanner.adapters.clamav import ClamAVScanner
from intelligent_reimbursement_assitant.scanner.adapters.format_probe import FormatProbe
from intelligent_reimbursement_assitant.scanner.adapters.local_artifact_store import (
    LocalArtifactStore,
)
from intelligent_reimbursement_assitant.scanner.application.upload_intake import UploadIntake


def create_app() -> FastAPI:
    settings = load_settings("api")
    return build_app(
        settings.database_url.get_secret_value(),
        cast(Path, settings.managed_directory),
        cast(ApiSettings, settings.api),
        cast(QASettings, settings.qa),
        cast(ApiSettings, settings.api).default_index_profile_id,
        cast(WikiSettings, settings.wiki).execution,
    )


def build_app(
    database_url: str,
    managed_directory: Path,
    settings: ApiSettings,
    qa_settings: QASettings,
    default_index_profile_id: str,
    wiki_execution: ExecutionSettings,
) -> FastAPI:
    try:
        engine = create_engine(
            database_url, connect_args={"connect_timeout": 2}, pool_pre_ping=True
        )
    except ArgumentError, ValueError:
        raise ValueError("api.database_url: connection_configuration") from None

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            yield
        finally:
            await asyncio.to_thread(engine.dispose)

    try:
        store = LocalArtifactStore(managed_directory / "quarantine")
        scanner = ClamAVScanner(
            store,
            policy=settings.policy,
            diagnostic_dir=managed_directory / "diagnostics",
            **settings.scanner.model_dump(),
        )
        intake = UploadIntake(store, FormatProbe(store.quarantine_dir), scanner)
        documents = SQLAlchemyDocumentRepository(engine)
        state = ExecutionStateService()
        service = DocumentIngestionService(
            documents, store, state, settings.execution, default_index_profile_id
        )
        events = SQLAlchemyEventRepository(engine)
        wiki_repository = SQLAlchemyWikiRepository(engine)
        qa_service = PolicyQAService(
            SQLAlchemyQARepository(engine),
            documents,
            state,
            qa_settings,
            wiki_repository,
        )
        app = FastAPI(lifespan=lifespan)
        install_document_ingestion(
            app,
            intake=intake,
            service=service,
            events=events,
            policy=settings.policy,
            max_concurrent_uploads=settings.max_concurrent_uploads,
            slot_wait_seconds=settings.slot_wait_seconds,
            reception_timeout_seconds=settings.reception_timeout_seconds,
        )
        install_policy_qa(app, qa_service, events)
        install_wiki(
            app,
            WikiService(
                wiki_repository, documents, state, wiki_execution, WikiArtifactReader(store)
            ),
        )
        return app
    except BaseException:
        engine.dispose()
        raise
