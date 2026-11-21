"""Executable projections shared by prompts and the model output boundary."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from intelligent_reimbursement_assitant.policy_qa_service.domain.models import (
    Coverage,
)

Nonblank = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


class TextFactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    meaning: Nonblank
    kind: Literal["text", "boolean"]


class NumberFactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    meaning: Nonblank
    kind: Literal["number"]
    value: Annotated[str, StringConstraints(pattern=r"^[+-]?[0-9]+(?:\.[0-9]+)?$")]
    unit: Nonblank


class DateFactResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    meaning: Nonblank
    kind: Literal["date"]
    date_from: Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]
    date_to: Annotated[str, StringConstraints(pattern=r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")]


FactResponse = Annotated[
    TextFactResponse | NumberFactResponse | DateFactResponse, Field(discriminator="kind")
]


class FactsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    facts: list[FactResponse]


class ConversationTextFactResponse(TextFactResponse):
    source: int


class ConversationNumberFactResponse(NumberFactResponse):
    source: int


class ConversationDateFactResponse(DateFactResponse):
    source: int


ConversationFactResponse = Annotated[
    ConversationTextFactResponse | ConversationNumberFactResponse | ConversationDateFactResponse,
    Field(discriminator="kind"),
]


class ConversationFactsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    facts: list[ConversationFactResponse]


class ItemResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    question: Nonblank
    kind: Literal["general_rule", "personal_application"]
    fact_refs: list[str]
    date_fact_ref: str | None
    missing_user_facts: list[Nonblank]


class PlanResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    items: list[ItemResponse]
    parse_issue: Literal["privacy_request", "prohibited_request", "unsupported_expression"] | None

    @model_validator(mode="after")
    def outcome(self) -> PlanResponse:
        if (self.parse_issue is None) != bool(self.items):
            raise ValueError("items must be nonempty on success and empty for parse_issue")
        return self


class ClarificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    question: Nonblank


class ClarificationItemUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    item_ref: str
    fact_refs: list[str]
    date_fact_ref: str | None
    missing_user_facts: list[Nonblank]


class ClarificationUpdateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    shared_replaced_fact_refs: list[str]
    updates: list[ClarificationItemUpdate]


class EvidenceGapResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    kind: Literal["USER_FACT", "POLICY_MISSING", "CONFLICT"]
    description: Nonblank
    chunk_refs: list[str]


class ComparisonResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    fact_ref: str
    operator: Literal["LE", "LT", "GE", "GT", "EQ"]
    policy_value: Annotated[str, StringConstraints(pattern=r"^[+-]?[0-9]+(?:\.[0-9]+)?$")]
    policy_unit: Nonblank
    policy_factor: Annotated[str, StringConstraints(pattern=r"^[+]?[0-9]+(?:\.[0-9]+)?$")]


class EvidenceClaimResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    text: Nonblank
    fact_refs: list[str]
    chunk_refs: list[str]
    comparisons: list[ComparisonResponse]


class EvidenceParseIssueResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    location: Nonblank
    reason: Nonblank


class AssessmentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    coverage: Coverage
    claims: list[EvidenceClaimResponse]
    gaps: list[EvidenceGapResponse]
    parse_issue: EvidenceParseIssueResponse | None


class EvidenceClarificationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    gap_refs: list[str]
    question: Nonblank | None


class AnswerSectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    text: Nonblank
    claim_refs: list[str]


class RetrievalFinishResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    chunk_refs: list[str]
    explanation: Nonblank
