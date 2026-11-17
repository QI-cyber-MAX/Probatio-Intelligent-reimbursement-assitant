from typing import Protocol

from intelligent_reimbursement_assitant.scanner.domain.models import QuarantinedUpload, ScanReport


class ScannerPort(Protocol):
    def scan(
        self, upload: QuarantinedUpload, *, deadline: float, detail_ref: str | None = None
    ) -> ScanReport: ...
