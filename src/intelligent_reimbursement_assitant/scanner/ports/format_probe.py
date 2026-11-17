from typing import Protocol

from intelligent_reimbursement_assitant.scanner.domain.models import UploadPolicy


class FileCheckPort(Protocol):
    def check_file(self, temp_key: str, policy: UploadPolicy, *, deadline: float) -> str: ...
