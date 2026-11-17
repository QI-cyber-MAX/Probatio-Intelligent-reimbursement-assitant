from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ConversationCreated(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    conversation_id: UUID


class QuestionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: UUID
    kb_id: UUID
    question: str = Field(min_length=1, max_length=10000)

    @field_validator("question")
    @classmethod
    def nonblank_question(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Question must contain non-whitespace characters")
        return value


class QAAccepted(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: UUID


class ClarificationReplyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: UUID
    text: str = Field(min_length=1, max_length=10000)

    @field_validator("text")
    @classmethod
    def nonblank_text(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("Reply must contain non-whitespace characters")
        return value
