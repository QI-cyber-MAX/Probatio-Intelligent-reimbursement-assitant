from decimal import Decimal
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import Engine, Uuid, case, func, insert, or_, select, true, tuple_, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.execution_state_service.adapters.events import (
    event_high_watermark,
)
from intelligent_reimbursement_assitant.execution_state_service.adapters.schema import execution_run
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionKind,
    ExecutionStatus,
)
from intelligent_reimbursement_assitant.policy_qa_service.adapters.schema import (
    agent_turn,
    conversation,
    conversation_message,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.errors import QAFailure
from intelligent_reimbursement_assitant.policy_qa_service.domain.models import (
    AgentTurn,
    AnswerResult,
    ClarificationMessage,
    ConversationHistoryPage,
    PublicQAMessage,
    QAJobView,
    QAMessage,
)


class SQLAlchemyQARepository:
    def __init__(self, engine: Engine):
        self.sessions = sessionmaker(engine.execution_options(isolation_level="READ COMMITTED"))

    def session(self) -> Session:
        return self.sessions()

    def create_conversation(self, session: Session, conversation_id: UUID) -> None:
        session.execute(
            insert(conversation).values(conversation_id=conversation_id, active_job_id=None)
        )

    def lock_conversation(self, session: Session, conversation_id: UUID) -> UUID | None:
        row = session.execute(
            select(conversation.c.active_job_id)
            .where(conversation.c.conversation_id == conversation_id)
            .with_for_update()
        ).one_or_none()
        if row is None:
            raise QAFailure(ErrorCode.CONVERSATION_NOT_FOUND, "会话不存在。")
        return row.active_job_id

    def find_message(self, session: Session, message_id: UUID) -> QAMessage | None:
        row = (
            session.execute(
                select(conversation_message).where(conversation_message.c.message_id == message_id)
            )
            .mappings()
            .one_or_none()
        )
        return QAMessage.model_validate(row) if row is not None else None

    def load_turn_by_message(self, session: Session, message_id: UUID) -> AgentTurn:
        row = (
            session.execute(select(agent_turn).where(agent_turn.c.message_id == message_id))
            .mappings()
            .one()
        )
        return AgentTurn.model_validate(row)

    def load_messages(self, session: Session, message_ids: list[UUID]) -> list[QAMessage]:
        if not message_ids:
            return []
        rows = session.execute(
            select(conversation_message).where(conversation_message.c.message_id.in_(message_ids))
        ).mappings()
        messages = {row["message_id"]: QAMessage.model_validate(row) for row in rows}
        # Missing persisted input is a read failure, never an empty history.
        return [messages[message_id] for message_id in message_ids]

    def lock_execution_status(self, session: Session, job_id: UUID) -> ExecutionStatus:
        return ExecutionStatus(
            session.execute(
                select(execution_run.c.status)
                .where(execution_run.c.job_id == job_id)
                .with_for_update()
            ).scalar_one()
        )

    def load_completed_history(
        self, session: Session, conversation_id: UUID, kb_id: UUID, turn_limit: int
    ) -> list[list[QAMessage]]:
        answer = conversation_message.alias("answer")
        roots = session.scalars(
            select(conversation_message.c.message_id)
            .where(
                conversation_message.c.conversation_id == conversation_id,
                conversation_message.c.payload["kind"].astext == "question",
                conversation_message.c.payload["snapshot"]["kb_id"].astext == str(kb_id),
                select(answer.c.message_id)
                .where(
                    answer.c.reply_to_message_id == conversation_message.c.message_id,
                    answer.c.payload["kind"].astext == "answer",
                )
                .exists(),
            )
            .order_by(
                conversation_message.c.created_at.desc(), conversation_message.c.message_id.desc()
            )
            .limit(turn_limit)
        ).all()
        if not roots:
            return []
        prompt = conversation_message.alias("prompt")
        prompt_ids = select(prompt.c.message_id).where(
            prompt.c.reply_to_message_id.in_(roots),
            prompt.c.payload["kind"].astext == "clarification_request",
        )
        rows = session.execute(
            select(conversation_message)
            .where(
                conversation_message.c.conversation_id == conversation_id,
                or_(
                    conversation_message.c.message_id.in_(roots),
                    (conversation_message.c.reply_to_message_id.in_(roots))
                    & (
                        conversation_message.c.payload["kind"].astext.in_(
                            ["answer", "clarification_request"]
                        )
                    ),
                    (conversation_message.c.reply_to_message_id.in_(prompt_ids))
                    & (conversation_message.c.payload["kind"].astext == "clarification_reply"),
                ),
            )
            .order_by(conversation_message.c.created_at, conversation_message.c.message_id)
        ).mappings()
        messages = [QAMessage.model_validate(row) for row in rows]
        prompt_roots = {
            m.message_id: m.reply_to_message_id
            for m in messages
            if m.payload.kind == "clarification_request"
        }
        groups: dict[UUID, list[QAMessage]] = {root: [] for root in roots}
        for message in messages:
            if message.payload.kind == "question":
                root = message.message_id
            elif message.payload.kind == "clarification_reply":
                root = cast(UUID, prompt_roots[cast(UUID, message.reply_to_message_id)])
            else:
                root = cast(UUID, message.reply_to_message_id)
            groups[root].append(message)
        return [groups[root] for root in roots]

    def insert_question_if_absent(self, session: Session, message: QAMessage) -> bool:
        return (
            session.scalar(
                pg_insert(conversation_message)
                .values(
                    message_id=message.message_id,
                    conversation_id=message.conversation_id,
                    reply_to_message_id=message.reply_to_message_id,
                    payload=message.payload.model_dump(mode="json"),
                    created_at=message.created_at,
                )
                .on_conflict_do_nothing(index_elements=[conversation_message.c.message_id])
                .returning(conversation_message.c.message_id)
            )
            is not None
        )

    def create_agent_turn(self, session: Session, turn: AgentTurn) -> None:
        session.execute(insert(agent_turn).values(**turn.model_dump()))

    def set_active_job(self, session: Session, conversation_id: UUID, job_id: UUID | None) -> None:
        session.execute(
            update(conversation)
            .where(conversation.c.conversation_id == conversation_id)
            .values(active_job_id=job_id)
        )

    def load_turn(self, session: Session, job_id: UUID) -> AgentTurn:
        row = (
            session.execute(
                select(agent_turn)
                .select_from(agent_turn.join(execution_run))
                .where(
                    agent_turn.c.qa_run_id == job_id,
                    execution_run.c.kind == ExecutionKind.AGENT_TURN,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise QAFailure(ErrorCode.QA_JOB_NOT_FOUND, "问答任务不存在。", stage="qa")
        return AgentTurn.model_validate(row)

    def read_event_head(self, session: Session, job_id: UUID) -> tuple[QAJobView, int] | None:
        row = (
            session.execute(
                select(
                    *[execution_run.c[name] for name in QAJobView.model_fields],
                    event_high_watermark(job_id).label("high_watermark"),
                )
                .select_from(execution_run.join(agent_turn))
                .where(
                    execution_run.c.job_id == job_id,
                    execution_run.c.kind == ExecutionKind.AGENT_TURN,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        return QAJobView.model_validate({key: row[key] for key in QAJobView.model_fields}), row[
            "high_watermark"
        ]

    def read_output(
        self, session: Session, job_id: UUID, kind: Literal["answer", "clarification"]
    ) -> tuple[QAJobView, QAMessage | None] | None:
        message = conversation_message.alias("output")
        if kind == "answer":
            condition = message.c.message_id == func.cast(agent_turn.c.answer_ref, Uuid)
        else:
            reply = conversation_message.alias("accepted_reply")
            condition = (
                (message.c.reply_to_message_id == agent_turn.c.message_id)
                & (message.c.payload["kind"].astext == "clarification_request")
                & ~select(reply.c.message_id)
                .where(
                    reply.c.reply_to_message_id == message.c.message_id,
                    reply.c.payload["kind"].astext == "clarification_reply",
                )
                .exists()
            )
        row = (
            session.execute(
                select(
                    *[execution_run.c[name] for name in QAJobView.model_fields],
                    agent_turn.c.message_id.label("root_id"),
                    agent_turn.c.conversation_id.label("expected_conversation"),
                    *[column.label("output_" + column.name) for column in message.c],
                )
                .select_from(execution_run.join(agent_turn).outerjoin(message, condition))
                .where(
                    execution_run.c.job_id == job_id,
                    execution_run.c.kind == ExecutionKind.AGENT_TURN,
                )
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            return None
        view = QAJobView.model_validate({key: row[key] for key in QAJobView.model_fields})
        output = (
            QAMessage.model_validate({key: row["output_" + key] for key in QAMessage.model_fields})
            if row["output_message_id"] is not None
            else None
        )
        required = view.status == (
            ExecutionStatus.SUCCEEDED if kind == "answer" else ExecutionStatus.WAITING_INPUT
        )
        if required and (
            output is None
            or output.conversation_id != row["expected_conversation"]
            or output.reply_to_message_id != row["root_id"]
            or not isinstance(
                output.payload, AnswerResult if kind == "answer" else ClarificationMessage
            )
        ):
            raise ValueError("Persisted QA output does not match its execution")
        return view, output

    def list_messages(
        self, session: Session, conversation_id: UUID, limit: int, before: UUID | None
    ) -> ConversationHistoryPage:
        cursor = conversation_message.alias("cursor_message")
        anchor = (
            select(
                conversation.c.conversation_id,
                conversation.c.active_job_id,
                cursor.c.message_id.label("cursor_id"),
                cursor.c.created_at.label("cursor_time"),
            )
            .select_from(
                conversation.outerjoin(
                    cursor,
                    (cursor.c.conversation_id == conversation.c.conversation_id)
                    & (cursor.c.message_id == before),
                )
            )
            .where(conversation.c.conversation_id == conversation_id)
            .cte("history_anchor")
        )
        page = (
            select(
                *[
                    conversation_message.c[name]
                    for name in PublicQAMessage.model_fields
                    if name != "payload"
                ],
                case(
                    (
                        conversation_message.c.payload["kind"].astext == "question",
                        func.jsonb_build_object(
                            "kind",
                            "question",
                            "question",
                            conversation_message.c.payload["question"],
                            "job_id",
                            agent_turn.c.qa_run_id,
                        ),
                    ),
                    else_=conversation_message.c.payload,
                ).label("payload"),
            )
            .select_from(
                conversation_message.outerjoin(
                    agent_turn, agent_turn.c.message_id == conversation_message.c.message_id
                )
            )
            .where(conversation_message.c.conversation_id == anchor.c.conversation_id)
        )
        if before is not None:
            page = page.where(
                tuple_(conversation_message.c.created_at, conversation_message.c.message_id)
                < tuple_(anchor.c.cursor_time, anchor.c.cursor_id)
            )
        selected = (
            page.order_by(
                conversation_message.c.created_at.desc(), conversation_message.c.message_id.desc()
            )
            .limit(limit + 1)
            .lateral()
        )
        rows = (
            session.execute(
                select(anchor, selected)
                .select_from(anchor.outerjoin(selected, true()))
                .order_by(selected.c.created_at.desc(), selected.c.message_id.desc())
            )
            .mappings()
            .all()
        )
        if not rows:
            raise QAFailure(ErrorCode.CONVERSATION_NOT_FOUND, "会话不存在。", stage="qa")
        if before is not None and rows[0]["cursor_id"] is None:
            raise QAFailure(
                ErrorCode.CONVERSATION_MESSAGE_NOT_FOUND, "游标消息不属于该会话。", stage="qa"
            )
        records = [row for row in rows if row["message_id"] is not None]
        messages = [
            PublicQAMessage.model_validate({key: row[key] for key in PublicQAMessage.model_fields})
            for row in reversed(records[:limit])
        ]
        return ConversationHistoryPage(
            conversation_id=conversation_id,
            active_job_id=rows[0]["active_job_id"],
            messages=messages,
            next_before=messages[0].message_id if len(records) > limit else None,
        )

    def load_execution_status(self, session: Session, job_id: UUID) -> ExecutionStatus:
        return ExecutionStatus(
            session.execute(
                select(execution_run.c.status).where(execution_run.c.job_id == job_id)
            ).scalar_one()
        )

    def load_budget_used(self, session: Session, job_id: UUID) -> Decimal:
        return session.execute(
            select(execution_run.c.budget_used).where(execution_run.c.job_id == job_id)
        ).scalar_one()

    def find_prompt(
        self, session: Session, root_id: UUID, interrupt_id: str | None = None
    ) -> QAMessage | None:
        query = select(conversation_message).where(
            conversation_message.c.reply_to_message_id == root_id,
            conversation_message.c.payload["kind"].astext == "clarification_request",
        )
        if interrupt_id is not None:
            query = query.where(
                conversation_message.c.payload["interrupt_id"].astext == interrupt_id
            )
        else:
            reply = conversation_message.alias("reply")
            query = query.where(
                ~select(reply.c.message_id)
                .where(
                    reply.c.reply_to_message_id == conversation_message.c.message_id,
                    reply.c.payload["kind"].astext == "clarification_reply",
                )
                .exists()
            )
        row = session.execute(query).mappings().one_or_none()
        return QAMessage.model_validate(row) if row is not None else None

    def find_reply(self, session: Session, prompt_id: UUID) -> QAMessage | None:
        row = (
            session.execute(
                select(conversation_message).where(
                    conversation_message.c.reply_to_message_id == prompt_id,
                    conversation_message.c.payload["kind"].astext == "clarification_reply",
                )
            )
            .mappings()
            .one_or_none()
        )
        return QAMessage.model_validate(row) if row is not None else None

    def insert_message(self, session: Session, message: QAMessage) -> None:
        session.execute(
            insert(conversation_message).values(
                message_id=message.message_id,
                conversation_id=message.conversation_id,
                reply_to_message_id=message.reply_to_message_id,
                payload=message.payload.model_dump(mode="json"),
                created_at=message.created_at,
            )
        )

    def set_answer_ref(self, session: Session, job_id: UUID, answer_ref: str) -> None:
        session.execute(
            update(agent_turn).where(agent_turn.c.qa_run_id == job_id).values(answer_ref=answer_ref)
        )

    def release_active_job(self, session: Session, conversation_id: UUID, job_id: UUID) -> None:
        session.execute(
            update(conversation)
            .where(
                conversation.c.conversation_id == conversation_id,
                conversation.c.active_job_id == job_id,
            )
            .values(active_job_id=None)
        )
