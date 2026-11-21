"""Atomic question acceptance; stable message identity reconciles uncertain commits."""

from datetime import UTC, datetime
from typing import Literal, cast
from uuid import UUID, uuid4, uuid5

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from intelligent_reimbursement_assitant.document_ingestion_service.ports.repository import (
    DocumentRepository,
)
from intelligent_reimbursement_assitant.errors.codes import ErrorCode as Code
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.errors.execution import public_terminal_reason
from intelligent_reimbursement_assitant.execution_state_service.adapters.events import (
    append_published_text,
)
from intelligent_reimbursement_assitant.execution_state_service.application.service import (
    TERMINAL,
    ExecutionStateService,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionContext,
    ExecutionKind,
    ExecutionStatus,
)
from intelligent_reimbursement_assitant.knowledge_wiki.ports.repository import WikiRepository
from intelligent_reimbursement_assitant.policy_qa_service.domain.config import QASettings
from intelligent_reimbursement_assitant.policy_qa_service.domain.errors import QAFailure
from intelligent_reimbursement_assitant.policy_qa_service.domain.models import (
    AgentTurn,
    AnswerResult,
    ClarificationMessage,
    ClarificationReply,
    ClarificationRequest,
    ConversationHistoryPage,
    QAInput,
    QAJobView,
    QAMessage,
)
from intelligent_reimbursement_assitant.policy_qa_service.ports.repository import QARepository


def _unavailable(stage: str = "qa") -> QAFailure:
    return QAFailure(
        Code.QA_UNAVAILABLE, "问答服务暂不可用，请稍后重试。", retryable=True, stage=stage
    )


def _unknown(stage: str = "qa") -> QAFailure:
    return QAFailure(
        Code.QA_ACCEPTANCE_OUTCOME_UNKNOWN,
        "受理结果尚未确认，请保留原消息 ID 等待核对。",
        stage=stage,
    )


class PolicyQAService:
    def __init__(
        self,
        repository: QARepository,
        documents: DocumentRepository,
        state_service: ExecutionStateService,
        settings: QASettings,
        wiki_repository: WikiRepository | None = None,
    ):
        self.wiki_repository = wiki_repository
        self.repository = repository
        self.documents = documents
        self.state_service = state_service
        self.settings = settings

    def load_understanding_messages(
        self, context_message_ids: list[UUID], reply_message_ids: list[UUID], kb_id: UUID
    ) -> tuple[list[QAMessage], list[QAMessage]]:
        messages = self.load_evidence_clarification_context(
            context_message_ids, reply_message_ids, kb_id
        )
        by_id = {message.message_id: message for message in messages}
        return (
            [by_id[message_id] for message_id in context_message_ids if message_id in by_id],
            [message for message in messages if message.message_id not in context_message_ids],
        )

    def create_conversation(self) -> UUID:
        conversation_id = uuid4()
        commit_started = False
        try:
            with self.repository.session() as session:
                self.repository.create_conversation(session, conversation_id)
                commit_started = True
                session.commit()
        except SQLAlchemyError as exc:
            raise (_unknown() if commit_started else _unavailable()) from exc
        return conversation_id

    def load_evidence_clarification_context(
        self, context_message_ids: list[UUID], reply_message_ids: list[UUID], kb_id: UUID
    ) -> list[QAMessage]:
        ids = list(dict.fromkeys(context_message_ids + reply_message_ids))
        with self.repository.session() as session:
            loaded = {m.message_id: m for m in self.repository.load_messages(session, ids)}
            # A reply points to a prompt, which points to its root question.
            for _ in range(2):
                parents = list(
                    dict.fromkeys(
                        m.reply_to_message_id
                        for m in loaded.values()
                        if m.reply_to_message_id is not None and m.reply_to_message_id not in loaded
                    )
                )
                loaded.update(
                    {m.message_id: m for m in self.repository.load_messages(session, parents)}
                )

        def allowed(message: QAMessage) -> bool:
            root = message
            while root.reply_to_message_id is not None:
                root = loaded[root.reply_to_message_id]
            return isinstance(root.payload, QAInput) and root.payload.snapshot.kb_id == kb_id

        requested = set(ids) | {loaded[k].reply_to_message_id for k in reply_message_ids}
        return sorted(
            [m for k, m in loaded.items() if k in requested and allowed(m)],
            key=lambda m: (m.created_at, m.message_id),
        )

    def ensure_history_scope(self, value: QAInput) -> None:
        messages = self.load_evidence_clarification_context(
            value.context_message_ids, [], value.snapshot.kb_id
        )
        if {m.message_id for m in messages} != set(value.context_message_ids):
            raise ExecutionError(
                Code.QA_STATE_CONFLICT,
                "PERMANENT",
                "Saved QA plan used another knowledge base; ask a new question",
            )

    def load_clarification_messages(
        self, reply_message_id: UUID
    ) -> tuple[ClarificationReply, ClarificationMessage]:
        with self.repository.session() as session:
            reply = self.repository.load_messages(session, [reply_message_id])[0]
            prompt = self.repository.load_messages(
                session, [cast(UUID, reply.reply_to_message_id)]
            )[0]
        # The acceptance boundary established both payload kinds and their relationship.
        return cast(ClarificationReply, reply.payload), cast(ClarificationMessage, prompt.payload)

    def _replay(
        self,
        session: Session,
        message: QAMessage,
        conversation_id: UUID,
        kb_id: UUID,
        question: str,
    ) -> AgentTurn:
        if (
            message.reply_to_message_id is not None
            or not isinstance(message.payload, QAInput)
            or message.conversation_id != conversation_id
            or message.payload.snapshot.kb_id != kb_id
            or message.payload.question != question
        ):
            raise QAFailure(Code.QUESTION_MESSAGE_CONFLICT, "消息 ID 已被不同内容使用。")
        return self.repository.load_turn_by_message(session, message.message_id)

    def accept_question(
        self,
        conversation_id: UUID,
        message_id: UUID,
        kb_id: UUID,
        question: str,
    ) -> AgentTurn:
        commit_started = False
        try:
            with self.repository.session() as session:
                active_job = self.repository.lock_conversation(session, conversation_id)
                existing = self.repository.find_message(session, message_id)
                if existing is not None:
                    return self._replay(session, existing, conversation_id, kb_id, question)
                if active_job is not None:
                    status = self.repository.lock_execution_status(session, active_job)
                    if status not in TERMINAL:
                        raise QAFailure(
                            Code.CONVERSATION_BUSY,
                            "会话仍有活动任务，请等待结束后重试。",
                            retryable=True,
                        )
                    self.repository.set_active_job(session, conversation_id, None)
                turns = self.repository.load_completed_history(
                    session, conversation_id, kb_id, self.settings.history_max_turns
                )
                context_ids = [
                    message.message_id
                    for message in sorted(
                        (message for turn in turns for message in turn),
                        key=lambda message: (message.created_at, message.message_id),
                    )
                ]
                snapshot = self.documents.load_knowledge_snapshot(session, kb_id)
                if snapshot is None:
                    raise QAFailure(Code.KNOWLEDGE_BASE_NOT_FOUND, "知识库不存在。")
                message = QAMessage(
                    message_id=message_id,
                    conversation_id=conversation_id,
                    reply_to_message_id=None,
                    payload=QAInput(
                        question=question,
                        context_message_ids=context_ids,
                        snapshot=snapshot,
                        wiki_ref=self.wiki_repository.find_published_ref(
                            session, kb_id, snapshot.revision
                        )
                        if self.wiki_repository is not None
                        else None,
                    ),
                    created_at=datetime.now(UTC),
                )
                if not self.repository.insert_question_if_absent(session, message):
                    winner = cast(QAMessage, self.repository.find_message(session, message_id))
                    return self._replay(session, winner, conversation_id, kb_id, question)
                job_id = self.state_service.enqueue_initial(
                    session, self.settings.execution, kind=ExecutionKind.AGENT_TURN
                )
                turn = AgentTurn(
                    qa_run_id=job_id,
                    conversation_id=conversation_id,
                    message_id=message_id,
                    input_ref=str(message_id),
                    answer_ref=None,
                )
                self.repository.create_agent_turn(session, turn)
                self.repository.set_active_job(session, conversation_id, job_id)
                commit_started = True
                session.commit()
                return turn
        except SQLAlchemyError as exc:
            if not commit_started:
                raise _unavailable() from exc
            # The failed write session is closed before opening this independent read.
            try:
                with self.repository.session() as session:
                    message = self.repository.find_message(session, message_id)
                    if message is not None:
                        return self._replay(session, message, conversation_id, kb_id, question)
            except SQLAlchemyError:
                pass
            # Absence alone cannot prove an in-flight COMMIT rolled back.
            raise _unknown() from exc

    def load_answer(self, job_id: UUID) -> AnswerResult | None:
        view, message = self._read_output(job_id, "answer")
        return (
            cast(AnswerResult, cast(QAMessage, message).payload)
            if view.status == ExecutionStatus.SUCCEEDED
            else None
        )

    def load_waiting_prompt(self, job_id: UUID) -> QAMessage:
        view, message = self._read_output(job_id, "clarification")
        if view.status != ExecutionStatus.WAITING_INPUT:
            raise QAFailure(Code.QA_STATE_CONFLICT, "任务当前没有等待中的澄清提示。", stage="qa")
        return cast(QAMessage, message)

    def _read_output(
        self, job_id: UUID, kind: Literal["answer", "clarification"]
    ) -> tuple[QAJobView, QAMessage | None]:
        try:
            with self.repository.session() as session:
                result = self.repository.read_output(session, job_id, kind)
        except SQLAlchemyError as exc:
            raise _unavailable("qa") from exc
        if result is None:
            raise QAFailure(Code.QA_JOB_NOT_FOUND, "问答任务不存在。", stage="qa")
        return result

    def get_answer(self, job_id: UUID) -> AnswerResult | None:
        view, message = self._read_output(job_id, "answer")
        if view.status in {ExecutionStatus.FAILED, ExecutionStatus.CANCELLED}:
            raise QAFailure(
                Code.QA_STATE_CONFLICT, "任务已失败或取消，无法读取正式回答。", stage="qa"
            )
        return (
            cast(AnswerResult, cast(QAMessage, message).payload)
            if view.status == ExecutionStatus.SUCCEEDED
            else None
        )

    def read_event_head(self, job_id: UUID) -> tuple[QAJobView, int]:
        with self.repository.session() as session:
            result = self.repository.read_event_head(session, job_id)
        if result is None:
            raise QAFailure(Code.QA_JOB_NOT_FOUND, "问答任务不存在。", stage="qa")
        return _public_job(result[0]), result[1]

    def get_run(self, job_id: UUID) -> QAJobView:
        try:
            return self.read_event_head(job_id)[0]
        except SQLAlchemyError as exc:
            raise _unavailable("qa") from exc

    def cancel_run(self, job_id: UUID) -> QAJobView:
        commit_started = False
        try:
            with self.repository.session() as session:
                turn = self.repository.load_turn(session, job_id)
                self.repository.lock_conversation(session, turn.conversation_id)
                self.state_service.request_cancel(session, job_id, kind=ExecutionKind.AGENT_TURN)
                result = cast(
                    tuple[QAJobView, int], self.repository.read_event_head(session, job_id)
                )[0]
                if result.status in TERMINAL:
                    self.repository.release_active_job(session, turn.conversation_id, job_id)
                commit_started = True
                session.commit()
                return _public_job(result)
        except SQLAlchemyError as exc:
            if commit_started:
                raise QAFailure(
                    Code.TASK_OPERATION_OUTCOME_UNKNOWN,
                    "取消结果尚未确认，请查询本任务。",
                    stage="qa",
                ) from exc
            raise _unavailable("qa") from exc

    def list_messages(
        self, conversation_id: UUID, limit: int, before: UUID | None
    ) -> ConversationHistoryPage:
        try:
            with self.repository.session() as session:
                return self.repository.list_messages(session, conversation_id, limit, before)
        except SQLAlchemyError as exc:
            raise _unavailable("qa") from exc

    def enter_waiting_input(
        self,
        ctx: ExecutionContext,
        interrupt_id: str,
        request: ClarificationRequest,
    ) -> QAMessage:
        try:
            with self.repository.session() as session:
                turn = self.repository.load_turn(session, ctx.job_id)
                self.repository.lock_conversation(session, turn.conversation_id)
                existing = self.repository.find_prompt(session, turn.message_id, interrupt_id)
                if (
                    existing is not None
                    and self.repository.load_execution_status(session, ctx.job_id)
                    == ExecutionStatus.WAITING_INPUT
                ):
                    return existing
                if not self.state_service.check_execution(session, ctx):
                    session.commit()
                    raise ExecutionError(Code.EXECUTION_LEASE_LOST, "ABORT", "Execution stopped")
                message = existing or QAMessage(
                    message_id=uuid5(turn.message_id, "interrupt:" + interrupt_id),
                    conversation_id=turn.conversation_id,
                    reply_to_message_id=turn.message_id,
                    payload=ClarificationMessage(interrupt_id=interrupt_id, request=request),
                    created_at=datetime.now(UTC),
                )
                if existing is None:
                    self.repository.insert_message(session, message)
                append_published_text(
                    session,
                    ctx.job_id,
                    message.message_id,
                    "clarification",
                    [(None, request.question)],
                )
                self.state_service.wait_for_input(session, ctx, str(message.message_id))
                session.commit()
                return message
        except SQLAlchemyError:
            with self.repository.session() as session:
                turn = self.repository.load_turn(session, ctx.job_id)
                message = self.repository.find_prompt(session, turn.message_id, interrupt_id)
                if (
                    message is not None
                    and self.repository.load_execution_status(session, ctx.job_id)
                    == ExecutionStatus.WAITING_INPUT
                ):
                    return message
            raise

    def accept_clarification_reply(
        self,
        job_id: UUID,
        prompt_message_id: UUID,
        message_id: UUID,
        text: str,
    ) -> AgentTurn:
        def replay(session: Session, turn: AgentTurn) -> bool:
            existing = self.repository.find_message(session, message_id)
            accepted = self.repository.find_reply(session, prompt_message_id)
            if existing is not None:
                if (
                    existing.conversation_id != turn.conversation_id
                    or existing.reply_to_message_id != prompt_message_id
                    or not isinstance(existing.payload, ClarificationReply)
                    or existing.payload.text != text
                ):
                    raise QAFailure(
                        Code.CLARIFICATION_MESSAGE_CONFLICT,
                        "回复消息 ID 已被不同内容使用。",
                        stage="qa",
                    )
                return True
            if accepted is not None:
                raise QAFailure(
                    Code.CLARIFICATION_MESSAGE_CONFLICT,
                    "该澄清提示已由另一条回复处理。",
                    stage="qa",
                )
            return False

        commit_started = False
        try:
            with self.repository.session() as session:
                turn = self.repository.load_turn(session, job_id)
                self.repository.lock_conversation(session, turn.conversation_id)
                prompt = self.repository.find_message(session, prompt_message_id)
                if (
                    prompt is None
                    or prompt.reply_to_message_id != turn.message_id
                    or not isinstance(prompt.payload, ClarificationMessage)
                ):
                    raise QAFailure(
                        Code.QA_CLARIFICATION_NOT_FOUND, "澄清提示不属于本任务。", stage="qa"
                    )
                if replay(session, turn):
                    return turn
                if (
                    self.repository.lock_execution_status(session, job_id)
                    != ExecutionStatus.WAITING_INPUT
                ):
                    raise QAFailure(Code.QA_STATE_CONFLICT, "任务当前没有等待输入。", stage="qa")
                current = self.repository.find_prompt(session, turn.message_id)
                if current is None or current.message_id != prompt_message_id:
                    raise QAFailure(
                        Code.QA_STATE_CONFLICT, "该澄清提示不再是当前等待项。", stage="qa"
                    )
                self.repository.insert_message(
                    session,
                    QAMessage(
                        message_id=message_id,
                        conversation_id=turn.conversation_id,
                        reply_to_message_id=prompt_message_id,
                        payload=ClarificationReply(text=text),
                        created_at=datetime.now(UTC),
                    ),
                )
                self.state_service.resume_input(session, job_id)
                commit_started = True
                session.commit()
                return turn
        except SQLAlchemyError as exc:
            try:
                with self.repository.session() as session:
                    turn = self.repository.load_turn(session, job_id)
                    if replay(session, turn):
                        return turn
            except SQLAlchemyError:
                pass
            raise (_unknown("qa") if commit_started else _unavailable("qa")) from exc

    def publish_answer(self, ctx: ExecutionContext, result: AnswerResult) -> AnswerResult:
        published = self.load_answer(ctx.job_id)
        if published is not None:
            return published
        try:
            with self.repository.session() as session:
                turn = self.repository.load_turn(session, ctx.job_id)
                self.repository.lock_conversation(session, turn.conversation_id)
                if not self.state_service.check_execution(session, ctx):
                    self.repository.release_active_job(session, turn.conversation_id, ctx.job_id)
                    session.commit()
                    raise ExecutionError(Code.EXECUTION_LEASE_LOST, "ABORT", "Execution stopped")
                message = QAMessage(
                    message_id=uuid5(turn.message_id, "answer"),
                    conversation_id=turn.conversation_id,
                    reply_to_message_id=turn.message_id,
                    payload=result,
                    created_at=datetime.now(UTC),
                )
                self.repository.insert_message(session, message)
                self.repository.set_answer_ref(session, ctx.job_id, str(message.message_id))
                append_published_text(
                    session,
                    ctx.job_id,
                    message.message_id,
                    "answer",
                    [(item.item_id, item.text) for item in result.items],
                )
                self.state_service.complete(session, ctx)
                self.repository.release_active_job(session, turn.conversation_id, ctx.job_id)
                session.commit()
                return result
        except SQLAlchemyError:
            published = self.load_answer(ctx.job_id)
            if published is not None:
                return published
            raise


def _public_job(result: QAJobView) -> QAJobView:
    return result.model_copy(
        update={
            "terminal_reason": public_terminal_reason(
                result.last_error_code, result.terminal_reason
            )
        }
    )
