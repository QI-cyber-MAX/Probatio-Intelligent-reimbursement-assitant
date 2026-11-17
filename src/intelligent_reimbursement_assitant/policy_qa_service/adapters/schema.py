"""Question storage shares task metadata; registration happens at composition roots."""

from sqlalchemy import Column, DateTime, ForeignKey, Index, Table, Text, Uuid, text
from sqlalchemy.dialects.postgresql import JSONB

from intelligent_reimbursement_assitant.execution_state_service.adapters.schema import metadata

conversation = Table(
    "conversation",
    metadata,
    Column("conversation_id", Uuid, primary_key=True),
    Column("active_job_id", Uuid, ForeignKey("execution_run.job_id")),
)
conversation_message = Table(
    "conversation_message",
    metadata,
    Column("message_id", Uuid, primary_key=True),
    Column("conversation_id", Uuid, ForeignKey("conversation.conversation_id"), nullable=False),
    Column("reply_to_message_id", Uuid, ForeignKey("conversation_message.message_id")),
    Column("payload", JSONB, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
agent_turn = Table(
    "agent_turn",
    metadata,
    Column("qa_run_id", Uuid, ForeignKey("execution_run.job_id"), primary_key=True),
    Column("conversation_id", Uuid, ForeignKey("conversation.conversation_id"), nullable=False),
    Column(
        "message_id",
        Uuid,
        ForeignKey("conversation_message.message_id"),
        nullable=False,
        unique=True,
    ),
    Column("input_ref", Text, nullable=False),
    Column("answer_ref", Text),
)
Index(
    "conversation_message_history",
    conversation_message.c.conversation_id,
    conversation_message.c.created_at,
    conversation_message.c.message_id,
)
Index(
    "conversation_message_one_answer",
    conversation_message.c.reply_to_message_id,
    unique=True,
    postgresql_where=text("payload->>'kind' = 'answer'"),
)
Index(
    "conversation_message_one_reply",
    conversation_message.c.reply_to_message_id,
    unique=True,
    postgresql_where=text("payload->>'kind' = 'clarification_reply'"),
)
Index(
    "conversation_message_one_interrupt",
    conversation_message.c.reply_to_message_id,
    text("(payload->>'interrupt_id')"),
    unique=True,
    postgresql_where=text("payload->>'kind' = 'clarification_request'"),
)
