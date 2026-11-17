from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class KnowledgeBaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1, max_length=200)

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class IngestionRebuildCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    new_job_id: UUID
