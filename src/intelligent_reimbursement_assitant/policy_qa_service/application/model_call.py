"""One request boundary for eligibility, accounting and bounded structural repair."""

import asyncio
import json
import re
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, JsonValue
from pydantic_core import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from intelligent_reimbursement_assitant.contracts.models import ModelUsage
from intelligent_reimbursement_assitant.errors.codes import ErrorCode as Code
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.errors.execution import retry_safe_operation
from intelligent_reimbursement_assitant.execution_state_service.application.service import (
    ExecutionStateService,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionContext,
)
from intelligent_reimbursement_assitant.model_gateway.domain.config import ModelSettings
from intelligent_reimbursement_assitant.model_gateway.domain.model_text import (
    restore_texts,
    share_texts,
)
from intelligent_reimbursement_assitant.model_gateway.ports.model_gateway import ModelGateway
from intelligent_reimbursement_assitant.policy_qa_service.domain.model_responses import (
    RetrievalFinishResponse,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.prompts import (
    REPAIR,
    encode,
    structured_messages,
)
from intelligent_reimbursement_assitant.policy_qa_service.ports.repository import QARepository


class _Message(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    content: str | None


class _Choice(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    finish_reason: str
    message: _Message


def _complete_content(raw: dict[str, JsonValue]) -> str:
    try:
        choices = raw["choices"]
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError("Expected one native choice")
        choice = _Choice.model_validate(choices[0])
    except (KeyError, ValueError, ValidationError) as error:
        raise ExecutionError(
            Code.QA_MODEL_OUTCOME_UNKNOWN, "OUTCOME_UNKNOWN", "Incomplete native response"
        ) from error
    if choice.finish_reason == "length":
        raise ExecutionError(Code.QA_MODEL_OUTPUT_LIMIT, "PERMANENT", "Model output limit reached")
    if choice.finish_reason == "content_filter":
        raise ExecutionError(Code.QA_TASK_UNPARSABLE, "PERMANENT", "Model content filtered")
    if choice.finish_reason != "stop":
        raise ExecutionError(
            Code.QA_MODEL_OUTCOME_UNKNOWN, "OUTCOME_UNKNOWN", "Model response interrupted"
        )
    if not choice.message.content or not choice.message.content.strip():
        raise ExecutionError(Code.QA_MODEL_INVALID_RESPONSE, "PERMANENT", "Model content is empty")
    return choice.message.content


async def check_execution(
    repository: QARepository,
    state_service: ExecutionStateService,
    ctx: ExecutionContext,
    *,
    defer_budget: bool = False,
) -> bool:
    def check() -> bool:
        try:
            with repository.session() as session:
                allowed = state_service.check_execution(session, ctx)
                if allowed or not defer_budget:
                    session.commit()
                else:
                    session.rollback()
                return allowed
        except SQLAlchemyError as error:
            raise ExecutionError(
                Code.QA_MODEL_OUTCOME_UNKNOWN, "OUTCOME_UNKNOWN", "Execution check unavailable"
            ) from error

    return await asyncio.to_thread(check)


async def _request(
    *,
    operation: Callable[[], Awaitable[tuple[dict[str, JsonValue], ModelUsage]]],
    capacity_input: object,
    repository: QARepository,
    state_service: ExecutionStateService,
    ctx: ExecutionContext,
    settings: ModelSettings,
    count_tokens: Callable[[str], int],
    check_eligibility: Callable[[], Awaitable[bool]] | None = None,
    on_response: Callable[[], None] | None = None,
    before_call: Callable[[], Awaitable[None]] | None = None,
) -> dict[str, JsonValue]:
    if (
        count_tokens(encode(capacity_input)) + settings.max_output_tokens
        > settings.context_window_tokens
    ):
        raise ExecutionError(
            Code.QA_PARSE_UNSUPPORTED, "PERMANENT", "Request exceeds model context capacity"
        )

    return await call_model(
        operation=operation,
        repository=repository,
        state_service=state_service,
        ctx=ctx,
        check_eligibility=check_eligibility,
        on_response=on_response,
        before_call=before_call,
    )


async def call_model[T](
    *,
    operation: Callable[[], Awaitable[tuple[T, ModelUsage]]],
    repository: QARepository,
    state_service: ExecutionStateService,
    ctx: ExecutionContext,
    check_eligibility: Callable[[], Awaitable[bool]] | None = None,
    on_response: Callable[[], None] | None = None,
    unknown_code: Code = Code.QA_MODEL_OUTCOME_UNKNOWN,
    before_call: Callable[[], Awaitable[None]] | None = None,
) -> T:
    """Settle an issued model operation and its actual usage before returning."""

    def account(input_tokens: int) -> None:
        try:
            with repository.session() as session:
                state_service.record_model_usage(session, ctx.job_id, input_tokens)
                session.commit()
        except SQLAlchemyError as error:
            # No transaction retry: COMMIT may already have applied the usage.
            raise ExecutionError(
                unknown_code, "OUTCOME_UNKNOWN", "Usage commit outcome unknown"
            ) from error

    async def guard() -> bool:
        return await (
            check_eligibility()
            if check_eligibility is not None
            else check_execution(repository, state_service, ctx)
        )

    async def request():
        if before_call is not None:
            await before_call()

        async def settle():
            raw, usage = await operation()
            # Queue the complete response before its independent PG usage commit.
            if on_response is not None:
                on_response()
            if usage.input_tokens is None:
                raise ExecutionError(unknown_code, "OUTCOME_UNKNOWN", "Input token usage unknown")
            await asyncio.to_thread(account, usage.input_tokens)
            return raw

        pending = asyncio.create_task(settle())
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            await pending
            raise

    return await retry_safe_operation(request, guard)


async def call_structured[Response: BaseModel, Result](
    *,
    messages: list[dict[str, JsonValue]],
    response_type: type[Response],
    assemble: Callable[[Response], Result],
    gateway: ModelGateway,
    repository: QARepository,
    state_service: ExecutionStateService,
    ctx: ExecutionContext,
    settings: ModelSettings,
    count_tokens: Callable[[str], int],
    structure_repair_limit: int,
    before_call: Callable[[], Awaitable[None]] | None = None,
) -> Result:
    schema = response_type.model_json_schema()
    original = messages
    current = messages
    shared_texts = {}
    for message in original:
        if message["role"] == "user":
            try:
                data = json.loads(cast(str, message["content"]))
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                shared_texts.update(data.get("texts", {}))

    for attempt in range(structure_repair_limit + 1):
        raw = await _request(
            operation=lambda request_messages=current: gateway.generate_structured(
                request_messages, schema
            ),
            capacity_input=structured_messages(current, schema),
            repository=repository,
            state_service=state_service,
            ctx=ctx,
            settings=settings,
            count_tokens=count_tokens,
            before_call=before_call,
        )
        content = _complete_content(raw)
        try:
            response = response_type.model_validate(
                restore_texts(json.loads(content), shared_texts)
            )
            return assemble(response)
        except (ValueError, ValidationError) as error:
            if attempt == structure_repair_limit:
                raise ExecutionError(
                    Code.QA_MODEL_INVALID_RESPONSE, "PERMANENT", "Invalid structured model response"
                ) from error
            if isinstance(error, ValidationError):
                details: object = error.errors(
                    include_url=False, include_context=False, include_input=False
                )
            else:
                details = str(error)

            def decode(value: str) -> object:
                try:
                    return json.loads(value)
                except json.JSONDecodeError:
                    return value

            inputs = [
                decode(cast(str, message["content"]))
                for message in original
                if message["role"] == "user"
            ]
            texts = dict(shared_texts)
            for value in inputs:
                if isinstance(value, dict):
                    texts.update(value.pop("texts", {}))
            data = share_texts(
                {
                    "inputs": inputs,
                    "texts": texts,
                    "repair": {"previous_output": decode(content), "errors": details},
                }
            )
            shared_texts.update(data.get("texts", {}))
            current = [
                *(message for message in original if message["role"] != "user"),
                {"role": "system", "content": REPAIR},
                {"role": "user", "content": encode(data)},
            ]
    raise AssertionError("unreachable")


class _Function(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    name: str
    arguments: str


class NativeCall(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: str
    type: Literal["function"]
    function: _Function


class _ToolMessage(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    role: Literal["assistant"]
    content: str | None
    reasoning_content: str | None = None
    tool_calls: list[NativeCall] = []


class _ToolChoice(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    finish_reason: str
    message: dict[str, JsonValue]


@dataclass(frozen=True)
class ToolResponse:
    message: dict[str, JsonValue]
    calls: list[NativeCall]
    errors: dict[str, str]


def tool_observation(call_id: str, content: object) -> dict[str, JsonValue]:
    return {"role": "tool", "tool_call_id": call_id, "content": encode(content)}


def pending_tool_calls(messages: list[dict[str, JsonValue]]) -> list[NativeCall]:
    completed = {cast(str, m["tool_call_id"]) for m in messages if m["role"] == "tool"}
    # Stored assistant calls have already passed the native response boundary.
    return [
        NativeCall.model_construct(
            id=call["id"],
            type="function",
            function=_Function.model_construct(**call["function"]),
        )
        for message in messages
        if message["role"] == "assistant"
        for call in cast(list[dict], message.get("tool_calls", []))
        if call["id"] not in completed
    ]


def consecutive_tool_response_errors(messages: list[dict[str, JsonValue]]) -> int:
    count = 0
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if message["role"] != "assistant":
            continue
        calls = cast(list[dict], message.get("tool_calls", []))
        ids = {call["id"] for call in calls}
        following = messages[index + 1 :]
        observations = [
            json.loads(cast(str, m["content"]))
            for m in following
            if m["role"] == "tool" and m["tool_call_id"] in ids
        ]
        missing = not calls
        if missing or any(
            "parameter_error" in value or "tool_response_error" in value for value in observations
        ):
            count += 1
        else:
            break
    return count


def _parse_tool_response(
    raw: dict[str, JsonValue],
    tools: list[dict[str, JsonValue]],
    messages: list[dict[str, JsonValue]],
    chunk_bindings: dict[str, str],
    policy_bindings: dict[str, str],
    wiki_bindings: dict[str, str] | None = None,
) -> ToolResponse:
    try:
        choices = raw["choices"]
        if not isinstance(choices, list) or len(choices) != 1:
            raise ValueError("Expected one choice")
        choice = _ToolChoice.model_validate(choices[0])
    except (KeyError, ValueError) as error:
        raise ExecutionError(
            Code.QA_MODEL_INVALID_RESPONSE, "PERMANENT", "Invalid native tool protocol"
        ) from error
    if choice.finish_reason == "length":
        raise ExecutionError(Code.QA_MODEL_OUTPUT_LIMIT, "PERMANENT", "Model output limit reached")
    if choice.finish_reason == "content_filter":
        raise ExecutionError(Code.QA_TASK_UNPARSABLE, "PERMANENT", "Model content filtered")
    if choice.finish_reason not in ("stop", "tool_calls"):
        raise ExecutionError(
            Code.QA_MODEL_OUTCOME_UNKNOWN, "OUTCOME_UNKNOWN", "Model response interrupted"
        )
    try:
        message = _ToolMessage.model_validate(choice.message)
    except ValueError as error:
        raise ExecutionError(
            Code.QA_MODEL_INVALID_RESPONSE, "PERMANENT", "Invalid native assistant message"
        ) from error
    calls = message.tool_calls
    ids = [call.id for call in calls]
    old_ids = {
        call["id"]
        for message in messages
        if message["role"] == "assistant"
        for call in cast(list[dict], message.get("tool_calls", []))
    }
    if (
        any(not value.strip() for value in ids)
        or len(ids) != len(set(ids))
        or old_ids.intersection(ids)
        or (choice.finish_reason == "tool_calls" and not calls)
    ):
        raise ExecutionError(
            Code.QA_MODEL_INVALID_RESPONSE, "PERMANENT", "Unassociable native tool calls"
        )
    definitions = {
        cast(dict, tool["function"])["name"]: cast(dict, tool["function"])["parameters"]
        for tool in tools
    }
    errors = {}
    counts = Counter(call.function.name for call in calls)
    for call in calls:
        try:
            schema = definitions.get(call.function.name)
            if schema is None:
                raise ValueError("Tool is not offered in this request")
            args = json.loads(call.function.arguments)
            if not isinstance(args, dict) or set(args) != set(schema["required"]):
                raise ValueError("Arguments must contain exactly the required fields")
            if call.function.name == "finish_retrieval":
                finish = RetrievalFinishResponse.model_validate(args)
                if len(calls) != 1:
                    raise ValueError("finish_retrieval must be the only call in a response")
                if re.search(r"【T[1-9][0-9]*】", finish.explanation):
                    raise ValueError(
                        "explanation must use self-contained prose, not temporary T references"
                    )
                refs = finish.chunk_refs
            elif call.function.name == "wiki_read":
                page_ref = args["page_ref"]
                if not isinstance(page_ref, str) or page_ref not in (wiki_bindings or {}):
                    raise ValueError("page_ref must select a supplied W label")
                refs = []
            elif call.function.name == "chunk_read":
                refs = args["chunk_refs"]
                if not isinstance(refs, list) or not refs:
                    raise ValueError("chunk_refs must select nonempty unique supplied options")
            else:
                if counts[call.function.name] > 1:
                    raise ValueError("Each search mode allows at most one query in a response")
                query = args["query"]
                if (
                    not isinstance(query, str)
                    or not query.strip()
                    or re.search(r"【T[1-9][0-9]*】", query)
                ):
                    raise ValueError("query must be nonblank self-contained text")
                policies = args["policy_refs"]
                if (
                    not isinstance(policies, list)
                    or any(
                        not isinstance(ref, str) or ref not in policy_bindings for ref in policies
                    )
                    or len(policies) != len(set(policies))
                ):
                    raise ValueError("policy_refs must select unique supplied options")
                refs = []
            if any(not isinstance(ref, str) or ref not in chunk_bindings for ref in refs) or len(
                refs
            ) != len(set(refs)):
                raise ValueError("chunk_refs must select unique supplied options")
        except (ValueError, TypeError) as error:
            errors[call.id] = str(error)
    return ToolResponse(choice.message, calls, errors)


async def call_tools(
    *,
    messages: list[dict[str, JsonValue]],
    tools: list[dict[str, JsonValue]],
    chunk_bindings: dict[str, str],
    policy_bindings: dict[str, str],
    gateway: ModelGateway,
    repository: QARepository,
    state_service: ExecutionStateService,
    ctx: ExecutionContext,
    settings: ModelSettings,
    count_tokens: Callable[[str], int],
    on_response: Callable[[], None],
    check_eligibility: Callable[[], Awaitable[bool]] | None = None,
    before_call: Callable[[], Awaitable[None]] | None = None,
    wiki_bindings: dict[str, str] | None = None,
) -> ToolResponse:
    raw = await _request(
        operation=lambda: gateway.decide_tools(messages, tools),
        capacity_input={"messages": messages, "tools": tools},
        repository=repository,
        state_service=state_service,
        ctx=ctx,
        settings=settings,
        count_tokens=count_tokens,
        check_eligibility=check_eligibility,
        on_response=on_response,
        before_call=before_call,
    )
    return _parse_tool_response(
        raw, tools, messages, chunk_bindings, policy_bindings, wiki_bindings
    )
