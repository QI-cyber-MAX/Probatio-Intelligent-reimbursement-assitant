from collections.abc import Awaitable, Callable
from typing import Protocol

from pydantic import JsonValue

from intelligent_reimbursement_assitant.contracts.models import ModelUsage


class RerankerPort(Protocol):
    async def rerank(
        self, question: str, texts: list[str], *, check_execution: Callable[[], Awaitable[bool]]
    ) -> tuple[dict[str, JsonValue], ModelUsage]: ...

    def parse_ranking(self, response: dict[str, JsonValue], candidate_count: int) -> list[int]: ...
