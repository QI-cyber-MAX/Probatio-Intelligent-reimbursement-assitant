from typing import Protocol

from intelligent_reimbursement_assitant.contracts.models import ModelUsage


class EmbeddingPort(Protocol):
    async def embed_documents(self, texts: list[str]) -> tuple[list[list[float]], ModelUsage]: ...

    async def embed_query(self, text: str) -> tuple[list[float], ModelUsage]: ...
