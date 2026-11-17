"""Explicit index provisioning; ingestion never creates physical indexes."""

import logging
from collections.abc import Mapping
from contextlib import ExitStack, closing
from typing import cast

from sqlalchemy import Engine, create_engine

from intelligent_reimbursement_assitant.content_index.adapters.elasticsearch import (
    ElasticsearchContentIndexAdapter,
)
from intelligent_reimbursement_assitant.content_index.domain.config import IndexProfileSettings
from intelligent_reimbursement_assitant.document_ingestion_service.adapters.repository import (
    SQLAlchemyDocumentRepository,
)
from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import Document
from intelligent_reimbursement_assitant.document_ingestion_service.domain.policy_metadata import (
    project_index_filter_metadata,
)
from intelligent_reimbursement_assitant.entrypoints.configuration import load_settings
from intelligent_reimbursement_assitant.errors.execution import exception_diagnostic


def initialize_indexes(
    profiles: Mapping[str, IndexProfileSettings], engine: Engine | None = None
) -> None:
    for profile in profiles.values():
        with closing(ElasticsearchContentIndexAdapter(profile.elasticsearch)) as adapter:
            if not adapter.client.indices.exists(index=adapter.index_name):
                adapter.create_index()
            else:
                adapter.client.indices.put_mapping(
                    index=adapter.index_name,
                    properties={
                        "filter_fields": {"properties": {"version_status": {"type": "keyword"}}},
                    },
                )
    if engine is not None:
        repository = SQLAlchemyDocumentRepository(engine)
        with repository.session() as session, ExitStack() as resources:
            adapters = {
                key: resources.enter_context(
                    closing(ElasticsearchContentIndexAdapter(profile.elasticsearch))
                )
                for key, profile in profiles.items()
            }
            for base in repository.list_knowledge_bases(session):
                snapshot = repository.load_knowledge_snapshot(session, base["kb_id"])
                if snapshot is None:
                    continue
                for entry in snapshot.entries:
                    document = repository.get_document(session, entry.source.document_id)
                    adapters[entry.index_profile_id].sync_filter_metadata(
                        entry.generation_id,
                        project_index_filter_metadata(
                            cast(Document, document), entry.policy_metadata
                        ),
                    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    failed = False
    try:
        settings = load_settings("migration")
        engine = create_engine(settings.database_url.get_secret_value())
        try:
            initialize_indexes(settings.index_profiles, engine)
        finally:
            engine.dispose()
    except Exception as error:
        # The CLI boundary must not expose credentials in SDK errors or chains.
        logging.error("Index initialization failed %s", exception_diagnostic(error))
        failed = True
    if failed:
        raise SystemExit(1)
    logging.info("Index initialization completed")


if __name__ == "__main__":
    main()
