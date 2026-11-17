"""Wiki Worker and metadata-only build/status commands."""

import argparse
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from functools import partial
from pathlib import Path
from typing import cast
from uuid import UUID

import httpx
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import ArgumentError

from intelligent_reimbursement_assitant.contracts.models import ArtifactRef
from intelligent_reimbursement_assitant.document_ingestion_service.adapters.repository import (
    SQLAlchemyDocumentRepository,
)
from intelligent_reimbursement_assitant.entrypoints.configuration import (
    AppSettings,
    ConfigurationError,
    load_settings,
    queue_binding,
)
from intelligent_reimbursement_assitant.errors.codes import ErrorCode as Code
from intelligent_reimbursement_assitant.errors.execution import exception_diagnostic
from intelligent_reimbursement_assitant.execution_state_service.application.service import (
    ExecutionStateService,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import ExecutionKind
from intelligent_reimbursement_assitant.knowledge_wiki.adapters.artifacts import WikiArtifactReader
from intelligent_reimbursement_assitant.knowledge_wiki.adapters.repository import (
    SQLAlchemyWikiRepository,
)
from intelligent_reimbursement_assitant.knowledge_wiki.application.executor import WikiExecutor
from intelligent_reimbursement_assitant.knowledge_wiki.application.service import enqueue_wiki_build
from intelligent_reimbursement_assitant.knowledge_wiki.domain.config import WikiSettings
from intelligent_reimbursement_assitant.model_gateway.adapters.deepseek import (
    DeepSeekModelGateway,
    ModelErrorCodes,
)
from intelligent_reimbursement_assitant.scanner.adapters.local_artifact_store import (
    LocalArtifactStore,
)
from intelligent_reimbursement_assitant.worker_runtime.entrypoints.worker import serve_worker


@asynccontextmanager
async def wiki_executor_factory(
    settings: AppSettings, engine: Engine, state: ExecutionStateService
):
    wiki = cast(WikiSettings, settings.wiki)
    store = LocalArtifactStore(cast(Path, settings.managed_directory) / "quarantine")
    async with httpx.AsyncClient(follow_redirects=False) as client:
        gateway = DeepSeekModelGateway(
            client,
            wiki.model,
            ModelErrorCodes(
                Code.WIKI_MODEL_CONFIGURATION_ERROR,
                Code.WIKI_MODEL_UNAVAILABLE,
                Code.WIKI_MODEL_OUTCOME_UNKNOWN,
            ),
        )
        yield WikiExecutor(
            SQLAlchemyWikiRepository(engine),
            state,
            store,
            WikiArtifactReader(store),
            gateway,
            wiki,
            tool_pool=asyncio.Semaphore(wiki.tool_concurrency),
        )


def _command(settings: AppSettings, args: argparse.Namespace) -> None:
    try:
        engine = create_engine(
            settings.database_url.get_secret_value(),
            connect_args={"connect_timeout": 2},
            pool_pre_ping=True,
        )
    except ArgumentError, ValueError:
        raise ConfigurationError("wiki", [("database_url", "connection_configuration")]) from None
    repository = SQLAlchemyWikiRepository(engine)
    try:
        if args.command == "build":
            job_id, reason = enqueue_wiki_build(
                repository,
                SQLAlchemyDocumentRepository(engine),
                ExecutionStateService(),
                cast(WikiSettings, settings.wiki).execution,
                args.kb_id,
                rebuild=args.rebuild,
            )
            print(
                json.dumps(
                    {"job_id": str(job_id) if job_id else None, "reason": reason},
                    ensure_ascii=False,
                )
            )
        else:
            with repository.session() as session:
                name, revision = repository.lock_knowledge_base(session, args.kb_id)
                published_build = repository.load_published_build(session, args.kb_id)
                published = repository.find_published_ref(session, args.kb_id, revision)
                latest = repository.load_latest_build(session, args.kb_id)
                successful = repository.load_latest_build(session, args.kb_id, successful=True)
            output = {
                "kb_id": str(args.kb_id),
                "name": name,
                "revision": revision,
                "published_revision": published_build["input"].snapshot.revision
                if published_build is not None
                else None,
                "published_matches_current_revision": published is not None,
                "latest_build": None,
                "latest_successful_revision": successful["input"].snapshot.revision
                if successful
                else None,
            }
            if latest is not None:
                output["latest_build"] = {
                    "job_id": str(latest["job_id"]),
                    "status": latest["status"],
                    "operation": "edit" if latest["input"].edited_pages is not None else "build",
                    "is_current": published_build is not None
                    and published_build["job_id"] == latest["job_id"],
                    "rebuild_sources": latest["input"].rebuild_sources,
                    "theme_source_updates": latest["checkpoint"].get("theme_source_updates", {}),
                    "revision": latest["input"].snapshot.revision,
                    "artifact_complete": bool(latest["checkpoint"].get("result_ref")),
                    "source_changed": latest["input"].snapshot.revision != revision,
                    "unfinished_steps": {
                        key: step
                        for key, step in cast(
                            dict[str, dict], latest["checkpoint"].get("steps", {})
                        ).items()
                    },
                }
                ref = latest["checkpoint"].get("result_ref")
                if ref is not None:
                    store = LocalArtifactStore(
                        cast(Path, settings.managed_directory) / "quarantine"
                    )
                    with store.open_artifact(ArtifactRef.model_validate(ref)) as source:
                        package = json.load(source)
                    output["parse_reports"] = package["parse_reports"]
                    output["open_questions"] = [
                        s
                        for p in package["pages"].values()
                        for s in p["statements"]
                        if s["kind"] == "open_question"
                    ]
            print(json.dumps(output, ensure_ascii=False))
    finally:
        engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command")
    for command in ("build", "status"):
        sub = commands.add_parser(command)
        sub.add_argument("--kb-id", required=True, type=UUID)
        if command == "build":
            sub.add_argument(
                "--rebuild",
                action="store_true",
                help="强制重析来源，保留当前发布包的场景和人工分类",
            )
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    settings = load_settings("wiki")
    wiki = cast(WikiSettings, settings.wiki)
    try:
        if args.command:
            _command(settings, args)
            return
        asyncio.run(
            serve_worker(
                settings.database_url.get_secret_value(),
                cast(SecretStr, settings.redis_url).get_secret_value(),
                cast(Path, settings.managed_directory),
                partial(wiki_executor_factory, settings),
                wiki.worker,
                binding=queue_binding(settings.queue_groups, ExecutionKind.WIKI_BUILD),
            )
        )
    except Exception as error:
        logging.error("Wiki Worker stopped %s", exception_diagnostic(error))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
