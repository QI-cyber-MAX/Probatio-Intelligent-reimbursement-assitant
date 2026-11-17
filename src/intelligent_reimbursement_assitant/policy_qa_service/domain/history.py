"""Business message text projected for understanding and clarification."""

from pydantic import JsonValue

from intelligent_reimbursement_assitant.policy_qa_service.domain.models import (
    AnswerResult,
    ClarificationMessage,
    ClarificationReply,
    QAInput,
    QAMessage,
)


def project_history_context(messages: list[QAMessage]) -> list[dict[str, JsonValue]]:
    result = []
    for message in messages:
        payload = message.payload
        entry: dict[str, JsonValue] = {"kind": payload.kind}
        match payload:
            case QAInput():
                entry["content"] = payload.question
            case AnswerResult():
                entry["content"] = "\n\n".join(
                    f"{item.question}\n{item.text}" for item in payload.items
                )
            case ClarificationMessage():
                entry["content"] = payload.request.question
            case ClarificationReply():
                entry["content"] = payload.text
        result.append(entry)
    return result


def project_exchanges(messages: list[QAMessage]) -> list[dict[str, str | None]]:
    replies = {
        message.reply_to_message_id: message.payload.text
        for message in messages
        if isinstance(message.payload, ClarificationReply)
    }
    return [
        {"question": message.payload.request.question, "reply": replies.get(message.message_id)}
        for message in messages
        if isinstance(message.payload, ClarificationMessage)
    ]
