from contextlib import AbstractContextManager
from typing import BinaryIO, Literal, Protocol

from intelligent_reimbursement_assitant.contracts.models import ArtifactRef
from intelligent_reimbursement_assitant.scanner.domain.models import QuarantinedUpload


class ArtifactStorePort(Protocol):
    def stage_stream(
        self, stream: BinaryIO, *, max_bytes: int, deadline: float
    ) -> tuple[str, int, str]: ...

    def open_read(self, temp_key: str) -> AbstractContextManager[BinaryIO]: ...

    def open_artifact(self, ref: ArtifactRef) -> AbstractContextManager[BinaryIO]: ...

    def write_artifact(
        self,
        content: bytes,
        *,
        media_type: str,
        artifact_kind: Literal["PARSED", "CHUNKED", "WIKI"],
    ) -> ArtifactRef: ...

    def discard(self, temp_key: str) -> None: ...

    def promote(self, upload: QuarantinedUpload) -> ArtifactRef: ...

    def discard_artifact(self, artifact_key: str) -> None: ...

    def cleanup_unreferenced_originals(
        self, *, cutoff_timestamp: float, referenced_keys: set[str]
    ) -> list[str]: ...
