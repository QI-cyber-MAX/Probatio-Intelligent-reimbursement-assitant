from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import UUID

from intelligent_reimbursement_assitant.contracts.models import ArtifactRef
from intelligent_reimbursement_assitant.parser.domain.models import ParsedDocument


class ParserPort(Protocol):
    async def parse(
        self,
        document_id: UUID,
        source_ref: ArtifactRef,
        *,
        check_execution: Callable[[], Awaitable[bool]],
    ) -> ParsedDocument: ...
