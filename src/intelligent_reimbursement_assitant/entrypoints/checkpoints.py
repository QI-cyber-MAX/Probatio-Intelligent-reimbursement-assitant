"""Explicit framework migration; never called during Worker startup."""

import asyncio

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from sqlalchemy.engine import make_url

from intelligent_reimbursement_assitant.entrypoints.configuration import load_settings


def checkpoint_conninfo(database_url: str) -> str:
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


async def setup_checkpoints(database_url: str) -> None:
    async with AsyncPostgresSaver.from_conn_string(checkpoint_conninfo(database_url)) as saver:
        await saver.setup()


def main() -> None:
    settings = load_settings("migration")
    asyncio.run(setup_checkpoints(settings.database_url.get_secret_value()))


if __name__ == "__main__":
    main()
