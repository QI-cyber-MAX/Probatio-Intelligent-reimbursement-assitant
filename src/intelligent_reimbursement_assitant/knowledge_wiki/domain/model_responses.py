"""Model response boundaries; identities and stage references are bound separately."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from intelligent_reimbursement_assitant.knowledge_wiki.domain.models import (
    TOPICS,
    EvidenceLevel,
    StatementKind,
    Topic,
)


class WikiStatementResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    kind: StatementKind
    topic: Topic
    evidence_level: EvidenceLevel
    text: str = Field(min_length=1)
    chunk_refs: list[str]
    related_page_refs: list[str]

    @model_validator(mode="after")
    def classify(self) -> WikiStatementResponse:
        expected = {"relationship": "relation", "open_question": "open_question"}.get(
            self.topic, "summary"
        )
        if self.kind != expected or (
            self.evidence_level == "inferred" and self.kind != "open_question"
        ):
            raise ValueError("Invalid kind/topic/evidence_level combination")
        if not self.text.strip():
            raise ValueError("text must be nonblank")
        return self


class WikiStatementsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    statements: list[WikiStatementResponse] = Field(max_length=6)

    @model_validator(mode="after")
    def topics(self) -> WikiStatementsResponse:
        selected = [s.topic for s in self.statements]
        if selected != [topic for topic in TOPICS if topic in selected]:
            raise ValueError("Topics must be unique and follow the specified order")
        return self


class WikiReviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    open_question_addition: WikiStatementResponse | None

    @model_validator(mode="after")
    def question_only(self) -> WikiReviewResponse:
        if (
            self.open_question_addition is not None
            and self.open_question_addition.topic != "open_question"
        ):
            raise ValueError("Review may only add open_question")
        return self


class WikiReferenceSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    statement_ref: str
    chunk_refs: list[str]


class WikiReferenceRebindingResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    statements: list[WikiReferenceSelection] = Field(max_length=6)


class WikiThemeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    title: str = Field(min_length=1)
    source_page_refs: list[str] = Field(min_length=1)

    @field_validator("title")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("title must be nonblank")
        return value


class WikiThemeClassificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    themes: list[WikiThemeResponse] = Field(min_length=1)


class WikiOverviewContextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    text: str = Field(min_length=1)
    related_page_refs: list[str] = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must be nonblank")
        return value


class WikiScenarioResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    page_ref: str = Field(min_length=1)
    text: str = Field(min_length=1)

    @field_validator("text")
    @classmethod
    def nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("text must be nonblank")
        return value


class WikiOverviewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    business_context: WikiOverviewContextResponse
    scenarios: list[WikiScenarioResponse]


class ChunkReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    chunk_refs: list[str] = Field(min_length=1)


class WikiReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    page_ref: str = Field(min_length=1)


class WikiExistingThemeResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    page_ref: str
    source_page_refs: list[str] = Field(min_length=1)


class WikiIncrementalClassificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    existing_themes: list[WikiExistingThemeResponse]
    new_themes: list[WikiThemeResponse]


class WikiThemeUpdateResponse(WikiStatementsResponse):
    removed_topics: list[Topic]

    @model_validator(mode="after")
    def disjoint(self) -> WikiThemeUpdateResponse:
        if len(set(self.removed_topics)) != len(self.removed_topics) or set(
            self.removed_topics
        ).intersection(s.topic for s in self.statements):
            raise ValueError("Updated and removed topics must be unique and disjoint")
        return self


class WikiOverviewUpdateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    business_context: WikiOverviewContextResponse | None
    scenarios: list[WikiScenarioResponse]


class WikiToolFunction(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    name: str
    arguments: str


class WikiToolCall(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: str = Field(min_length=1)
    type: Literal["function"]
    function: WikiToolFunction


class WikiAssistantMessage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    role: Literal["assistant"]
    content: str | None
    reasoning_content: str | None = None
    tool_calls: list[WikiToolCall] = Field(default_factory=list)
