"""One-shot deployment initialization, separate from application startup."""

import asyncio
import logging

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine

from intelligent_reimbursement_assitant.entrypoints.checkpoints import setup_checkpoints
from intelligent_reimbursement_assitant.entrypoints.configuration import load_settings
from intelligent_reimbursement_assitant.entrypoints.indexes import initialize_indexes
from intelligent_reimbursement_assitant.errors.execution import exception_diagnostic


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    stage = "configuration"
    failed = False
    try:
        settings = load_settings("migration")
        database_url = settings.database_url.get_secret_value()
        stage = "business migrations"
        logging.info("Initializing %s", stage)
        config = Config("alembic.ini")
        config.attributes["database_url"] = database_url
        command.upgrade(config, "head")
        stage = "native checkpoints"
        logging.info("Initializing %s", stage)
        asyncio.run(setup_checkpoints(database_url))
        stage = "content indexes"
        logging.info("Initializing %s", stage)
        engine = create_engine(database_url)
        try:
            initialize_indexes(settings.index_profiles, engine)
        finally:
            engine.dispose()
    except Exception as error:
        # Catch only at the process boundary, without printing third-party text.
        logging.error("Initialization failed stage=%s %s", stage, exception_diagnostic(error))
        failed = True
    if failed:
        raise SystemExit(1)
    logging.info("Initialization completed")


if __name__ == "__main__":
    main()
