"""Route by the scanner-confirmed MIME and configured PDF backend."""

from collections.abc import Awaitable, Callable
from uuid import UUID

from intelligent_reimbursement_assitant.contracts.models import ArtifactRef
from intelligent_reimbursement_assitant.parser.adapters.docling.adapter import DoclingParserAdapter
from intelligent_reimbursement_assitant.parser.adapters.mineru.adapter import MinerUParserAdapter
from intelligent_reimbursement_assitant.parser.domain.models import ParsedDocument
from intelligent_reimbursement_assitant.scanner.domain.models import PDF


class RoutingParserAdapter:
    def __init__(self, office: DoclingParserAdapter, cloud: MinerUParserAdapter | None):
        self.office = office
        self.cloud = cloud

    async def parse(
        self,
        document_id: UUID,
        source_ref: ArtifactRef,
        *,
        check_execution: Callable[[], Awaitable[bool]],
    ) -> ParsedDocument:
        parser = (
            self.cloud if source_ref.media_type == PDF and self.cloud is not None else self.office
        )
        return await parser.parse(document_id, source_ref, check_execution=check_execution)

    async def aclose(self) -> None:
        try:
            if self.cloud is not None:
                await self.cloud.aclose()
        finally:
            await self.office.aclose()
