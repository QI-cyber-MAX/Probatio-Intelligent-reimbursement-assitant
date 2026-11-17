"""Date authority, conservative date bounds and index/model projections."""

from calendar import monthrange
from datetime import date

from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    Document,
    IndexFilterMetadata,
    PolicyDate,
    PolicyDateSource,
    PolicyMetadata,
    SourceMetadataUpdate,
)


def user_policy_date(value: date | None) -> PolicyDate | None:
    return PolicyDate(value=value.isoformat(), source=PolicyDateSource.USER) if value else None


def date_bounds(value: PolicyDate) -> tuple[date, date]:
    if len(value.value) == 10:
        day = date.fromisoformat(value.value)
        return day, day
    year, month = map(int, value.value.split("-"))
    return date(year, month, 1), date(year, month, monthrange(year, month)[1])


def overlaps_policy(policy: PolicyMetadata, start: date, end: date) -> bool:
    return (policy.effective_from is None or date_bounds(policy.effective_from)[0] < end) and (
        policy.effective_to is None or date_bounds(policy.effective_to)[1] > start
    )


def model_policy_date(value: PolicyDate | None) -> dict[str, str] | None:
    return {"value": value.value, "source": value.source.value} if value else None


def resolve_policy_metadata(
    current: PolicyMetadata,
    extracted: PolicyDate | None,
    update: SourceMetadataUpdate | None = None,
) -> PolicyMetadata:
    start = current.effective_from
    end = current.effective_to
    status = current.version_status
    if update is None:
        if start is None or start.source != PolicyDateSource.USER:
            start = extracted
    else:
        fields = update.model_fields_set
        if "effective_from" in fields:
            start = user_policy_date(update.effective_from) if update.effective_from else extracted
        if "effective_to" in fields:
            end = user_policy_date(update.effective_to)
        if update.version_status is not None:
            status = update.version_status
    if start and end and len(start.value) == len(end.value) == 10 and end.value <= start.value:
        raise ValueError("effective_to must be later than effective_from")
    return current.model_copy(
        update={
            "effective_from": start,
            "effective_to": end,
            "version_status": status,
        }
    )


def project_index_filter_metadata(
    document: Document, policy: PolicyMetadata
) -> IndexFilterMetadata:
    def day(value: PolicyDate | None) -> date | None:
        return date.fromisoformat(value.value) if value and len(value.value) == 10 else None

    return IndexFilterMetadata(
        kb_id=document.kb_id,
        title=document.title,
        version_status=policy.version_status,
        effective_from=day(policy.effective_from),
        effective_to=day(policy.effective_to),
    )
