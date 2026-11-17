"""Compute source version transitions under the logical document publication lock."""

from collections.abc import Sequence

from intelligent_reimbursement_assitant.contracts.models import SourceReference
from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    PolicyMetadata,
    SnapshotEntry,
    VersionStatus,
)


def compute_version_updates(
    published: Sequence[SnapshotEntry],
    source: SourceReference,
    policy: PolicyMetadata,
    promote_current: bool,
) -> dict[SourceReference, PolicyMetadata]:
    updates: dict[SourceReference, PolicyMetadata] = {}
    updates[source] = policy
    if promote_current:
        for entry in published:
            if (
                entry.source != source
                and entry.policy_metadata.version_status == VersionStatus.CURRENT
            ):
                updates[entry.source] = entry.policy_metadata.model_copy(
                    update={"version_status": VersionStatus.HISTORICAL}
                )
    return updates
