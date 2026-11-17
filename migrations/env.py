"""Explicit migrations only; application startup never creates or migrates tables."""

from alembic import context
from sqlalchemy import Connection, create_engine, pool
from sqlalchemy.exc import ArgumentError

from intelligent_reimbursement_assitant.document_ingestion_service.adapters.schema import metadata
from intelligent_reimbursement_assitant.entrypoints.configuration import load_settings
from intelligent_reimbursement_assitant.knowledge_wiki.adapters import (
    schema as wiki_schema,  # noqa: F401
)
from intelligent_reimbursement_assitant.policy_qa_service.adapters import (
    schema as qa_schema,  # noqa: F401
)

config = context.config


def run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=metadata)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    url = (
        config.attributes.get("database_url")
        or load_settings("migration").database_url.get_secret_value()
    )
    try:
        context.configure(url=url, target_metadata=metadata, literal_binds=True)
    except ArgumentError, ValueError:
        raise ValueError("migration.database_url: connection_configuration") from None
    with context.begin_transaction():
        context.run_migrations()
elif "connection" in config.attributes:
    run_migrations(config.attributes["connection"])
else:
    url = (
        config.attributes.get("database_url")
        or load_settings("migration").database_url.get_secret_value()
    )
    try:
        engine = create_engine(url, poolclass=pool.NullPool)
    except ArgumentError, ValueError:
        raise ValueError("migration.database_url: connection_configuration") from None
    try:
        with engine.connect() as connection:
            run_migrations(connection)
    finally:
        engine.dispose()
