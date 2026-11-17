from uuid import UUID

from pydantic import BaseModel, ConfigDict

from intelligent_reimbursement_assitant.knowledge_wiki.domain.models import (
    WikiOverviewEdit,
    WikiSceneEdit,
    WikiSourcePageEdit,
)


class WikiEditCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    new_job_id: UUID
    base_job_id: UUID
    source_pages: list[WikiSourcePageEdit]
    scenes: list[WikiSceneEdit]
    overview: WikiOverviewEdit


class WikiEditAccepted(BaseModel):
    job_id: UUID
