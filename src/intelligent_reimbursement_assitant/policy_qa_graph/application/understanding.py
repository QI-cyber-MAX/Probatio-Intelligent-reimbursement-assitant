"""Question understanding and clarification nodes with injected model and storage ports."""

import asyncio
import logging
from collections.abc import Callable
from functools import partial
from typing import TypeVar, cast
from uuid import UUID
from zoneinfo import ZoneInfo

from pydantic import BaseModel, JsonValue

from intelligent_reimbursement_assitant.errors.codes import ErrorCode as Code
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.errors.execution import run_io
from intelligent_reimbursement_assitant.execution_state_service.application.service import (
    ExecutionStateService,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.events import (
    ProgressFactory,
    ProgressStage,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionContext,
)
from intelligent_reimbursement_assitant.knowledge_wiki.ports.reading import WikiReaderPort
from intelligent_reimbursement_assitant.model_gateway.domain.config import ModelSettings
from intelligent_reimbursement_assitant.model_gateway.domain.model_text import share_texts
from intelligent_reimbursement_assitant.model_gateway.ports.model_gateway import ModelGateway
from intelligent_reimbursement_assitant.policy_qa_graph.domain.state import QAState
from intelligent_reimbursement_assitant.policy_qa_service.application.model_call import (
    call_structured,
    check_execution,
)
from intelligent_reimbursement_assitant.policy_qa_service.application.service import PolicyQAService
from intelligent_reimbursement_assitant.policy_qa_service.domain.config import (
    QASettings,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.history import (
    project_exchanges,
    project_history_context,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.model_inputs import (
    bind,
    fact_context,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.model_responses import (
    ClarificationResponse,
    ClarificationUpdateResponse,
    ConversationFactsResponse,
    FactsResponse,
    PlanResponse,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.models import (
    ClarificationReply,
    ClarificationRequest,
    QAInput,
    TaskPlan,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.prompts import (
    CLARIFICATION_FACTS,
    CLARIFICATION_UPDATE,
    CLARIFY,
    COMMON,
    FACTS,
    PLAN,
    encode,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.tool_calling import (
    build_policy_catalog,
    select_snapshot_entries,
    snapshot_groups,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.understanding import (
    apply_clarification_update,
    assemble_conversation_facts,
    assemble_task_plan,
    assemble_user_facts,
    build_date_scopes,
    build_fact_references,
    fact_extraction_input,
    item_context,
    plan_input,
    select_clarification,
)

Response = TypeVar("Response", bound=BaseModel)
Result = TypeVar("Result")


async def understand(
    state: QAState,
    ctx: ExecutionContext,
    *,
    service: PolicyQAService,
    gateway: ModelGateway,
    state_service: ExecutionStateService,
    settings: QASettings,
    model_settings: ModelSettings,
    business_timezone: ZoneInfo,
    count_tokens: Callable[[str], int],
    rebuild: bool = False,
    progress: ProgressFactory | None = None,
    wiki_reader: WikiReaderPort | None = None,
    io_pool: asyncio.Semaphore | None = None,
) -> QAState:
    """Return a whole replacement only after every required request has succeeded.

    A saved output is reused. The executor calls rebuild=True only when the user
    changes the solving goal and requires new or split items.
    """
    if state["plan"] is not None and not rebuild:
        return state
    history, replies = await asyncio.to_thread(
        service.load_understanding_messages,
        state["input"].context_message_ids,
        state["reply_message_ids"],
        state["input"].snapshot.kb_id,
    )
    known = {message.message_id: message for message in [*history, *replies]}
    messages = project_history_context(list(known.values()))
    sources_by_id = {message_id: index for index, message_id in enumerate(known)}
    sources_by_id[state["message_id"]] = len(messages)
    messages.append({"kind": "question", "content": state["input"].question, "root": True})
    # Accepted replies follow their parent prompt; selected history retains its order.
    if replies:
        ordered_ids = (
            [m.message_id for m in history]
            + [state["message_id"]]
            + [m.message_id for m in replies if m.message_id not in {h.message_id for h in history}]
        )
        original = {key: messages[index] for key, index in sources_by_id.items()}
        messages = [original[key] for key in ordered_ids]
        sources_by_id = {key: index for index, key in enumerate(ordered_ids)}

    async def call(
        prompt: str,
        data: object,
        response_type: type[Response],
        assemble: Callable[[Response], Result],
    ) -> Result:
        messages: list[dict[str, JsonValue]] = [
            {"role": "system", "content": COMMON + prompt},
            {"role": "user", "content": encode(data)},
        ]
        return await call_structured(
            messages=messages,
            response_type=response_type,
            assemble=assemble,
            gateway=gateway,
            repository=service.repository,
            state_service=state_service,
            ctx=ctx,
            settings=model_settings,
            count_tokens=count_tokens,
            structure_repair_limit=settings.structure_repair_limit,
            before_call=partial(progress(ctx, None, None), ProgressStage.UNDERSTANDING)
            if progress
            else None,
        )

    user_sources = {
        index: message_id
        for message_id, index in sources_by_id.items()
        if message_id == state["message_id"]
        or isinstance(known[message_id].payload, (QAInput, ClarificationReply))
    }
    facts, values = await call(
        FACTS,
        fact_extraction_input(messages, state["reference_time"], business_timezone),
        ConversationFactsResponse,
        lambda response: assemble_conversation_facts(response, user_sources),
    )
    scopes = build_date_scopes(facts, values)

    wiki_overview = None
    if wiki_reader is not None and state["input"].wiki_ref is not None:
        page_ref = state["input"].wiki_ref
        load_page = wiki_reader.load_page
        try:
            raw_page = await run_io(
                lambda: asyncio.to_thread(
                    load_page,
                    page_ref,
                    state["input"].snapshot.kb_id,
                    "overview",
                    state["input"].snapshot.entries,
                ),
                io_pool or asyncio.Semaphore(settings.tool_concurrency),
                partial(check_execution, service.repository, state_service, ctx),
            )
            overview, _, _ = wiki_reader.project_page(
                raw_page, {}, {}, build_policy_catalog(state["input"].snapshot.entries, {})[1]
            )
            wiki_overview = (
                "\n".join(
                    statement["text"]
                    for statements in overview["sections"].values()
                    for statement in statements
                    if statement["kind"] == "summary"
                )
                or None
            )
        except ExecutionError as error:
            if error.code not in (Code.WIKI_SOURCE_UNAVAILABLE, Code.WIKI_ARTIFACT_ERROR):
                raise
            logging.getLogger(__name__).warning(
                "Wiki overview unavailable during task planning job_id=%s ref=%s code=%s",
                ctx.job_id,
                state["input"].wiki_ref.key,
                error.code,
            )

    def assemble(response: PlanResponse) -> tuple[TaskPlan, PlanResponse]:
        return assemble_task_plan(response, facts, scopes), response

    plan, projected = await call(
        PLAN,
        plan_input(messages, facts, sources_by_id, values, wiki_overview=wiki_overview),
        PlanResponse,
        assemble,
    )
    item_ids, missing = select_clarification(
        plan, projected, state["clarification_count"], settings.clarification_limit
    )
    clarification = None
    if missing:
        if len(missing) == 1 and len(plan.items) == 1:
            question = f"请补充：{missing[0]}。"
        else:
            question = await call(
                CLARIFY,
                share_texts(
                    {
                        "exchanges": project_exchanges([*history, *replies]),
                        "items": [
                            {
                                **item_context(item, plan.facts, values),
                                "missing_user_facts": projected.items[index].missing_user_facts,
                            }
                            for index, item in enumerate(plan.items)
                        ],
                    }
                ),
                ClarificationResponse,
                lambda response: response.question,
            )
        clarification = ClarificationRequest(item_ids=item_ids, question=question)
        plan = plan.model_copy(update={"clarification": clarification})
    return {
        **state,
        "plan": plan,
        "fact_values": {
            fact.fact_id: values[fact.fact_id] for fact in plan.facts if fact.fact_id in values
        },
        "clarification": clarification,
        "search_queries": [],
        "tool_sessions": {},
        "chunk_history_by_item": {},
        "decision": None,
        "pending_tool_executions": [],
        "search_batches": [],
        "ranked_chunk_ids": {},
        "evidence_ids_by_item": {},
        "retrieval_notes": {},
        "assessments": [],
        "answer_sections": [],
        "result": None,
    }


async def apply_clarification(
    state: QAState,
    ctx: ExecutionContext,
    *,
    reply_message_id: UUID,
    service: PolicyQAService,
    gateway: ModelGateway,
    state_service: ExecutionStateService,
    settings: QASettings,
    model_settings: ModelSettings,
    business_timezone: ZoneInfo,
    count_tokens: Callable[[str], int],
    progress: ProgressFactory | None = None,
) -> QAState:
    old_plan = cast(TaskPlan, state["plan"])
    if reply_message_id in state["reply_message_ids"]:
        return state
    values = dict(state["fact_values"])
    reply, prompt = await asyncio.to_thread(service.load_clarification_messages, reply_message_id)
    background = {"clarification": prompt.request.question, "reply": reply.text}

    async def call(
        instruction: str,
        data: object,
        response_type: type[Response],
        assemble: Callable[[Response], Result],
    ) -> Result:
        return await call_structured(
            messages=[
                {"role": "system", "content": COMMON + instruction},
                {"role": "user", "content": encode(data)},
            ],
            response_type=response_type,
            assemble=assemble,
            gateway=gateway,
            repository=service.repository,
            state_service=state_service,
            ctx=ctx,
            settings=model_settings,
            count_tokens=count_tokens,
            structure_repair_limit=settings.structure_repair_limit,
            before_call=partial(progress(ctx, None, None), ProgressStage.APPLYING_CLARIFICATION)
            if progress
            else None,
        )

    new_facts, new_values = await call(
        CLARIFICATION_FACTS,
        share_texts(
            {
                "clarification": prompt.request.question,
                "reply": reply.text,
                "reference_time": state["reference_time"].isoformat(),
                "business_timezone": str(business_timezone),
                "items": [
                    item_context(item, old_plan.facts, values)
                    for item in old_plan.items
                    if item.item_id in prompt.request.item_ids
                ],
            }
        ),
        FactsResponse,
        lambda response: assemble_user_facts(reply_message_id, response),
    )
    values.update(new_values)
    references = build_fact_references(old_plan)
    scopes = build_date_scopes(old_plan.facts + new_facts, values)

    def assemble(response: ClarificationUpdateResponse):
        plan, changed = apply_clarification_update(
            old_plan, response, new_facts, scopes, set(prompt.request.item_ids), references
        )
        return plan, changed, response

    plan, changed, response = await call(
        CLARIFICATION_UPDATE,
        share_texts(
            {
                **background,
                "items": {
                    ref: {
                        "question": item.question,
                        "kind": item.kind,
                        "fact_refs": [
                            key
                            for key, fact in bind("F", old_plan.facts + new_facts).items()
                            if fact.fact_id in item.fact_ids
                        ],
                        "target": item.item_id in prompt.request.item_ids,
                    }
                    for ref, item in bind("I", old_plan.items).items()
                },
                "facts": {
                    ref: {
                        "origin": "old" if index < len(old_plan.facts) else "reply",
                        **fact_context(fact, values),
                    }
                    for index, (ref, fact) in enumerate(
                        bind("F", old_plan.facts + new_facts).items()
                    )
                },
            }
        ),
        ClarificationUpdateResponse,
        assemble,
    )
    missing_by_item = {
        bind("I", old_plan.items)[update.item_ref].item_id: update.missing_user_facts
        for update in response.updates
    }
    clarification = None
    if state["clarification_count"] < settings.clarification_limit and all(
        item.kind == "personal_application" and missing_by_item.get(item.item_id)
        for item in plan.items
    ):
        missing = list(
            dict.fromkeys(value for values in missing_by_item.values() for value in values)
        )
        if len(plan.items) == len(missing) == 1:
            question = f"请补充：{missing[0]}。"
        else:
            exchanges = await asyncio.to_thread(
                service.load_evidence_clarification_context,
                state["input"].context_message_ids,
                [*state["reply_message_ids"], reply_message_id],
                state["input"].snapshot.kb_id,
            )
            question = await call(
                CLARIFY,
                share_texts(
                    {
                        "exchanges": project_exchanges(exchanges),
                        "items": [
                            {
                                **item_context(item, plan.facts, values),
                                "missing_user_facts": missing_by_item[item.item_id],
                            }
                            for item in plan.items
                        ],
                    }
                ),
                ClarificationResponse,
                lambda value: value.question,
            )
        clarification = ClarificationRequest(
            item_ids=[item.item_id for item in plan.items], question=question
        )
        plan = plan.model_copy(update={"clarification": clarification})
    by_item = {item.item_id: item for item in plan.items}
    groups = {
        key: snapshot_groups(select_snapshot_entries(by_item[key], state["input"].snapshot))
        for key in changed
    }
    query_items = {query.query_id: query.item_id for query in state["search_queries"]}
    batches = [
        batch for batch in state["search_batches"] if query_items[batch.query_id] not in changed
    ]
    evidence_ids = dict(state["evidence_ids_by_item"])
    for item_id in changed:
        allowed = set().union(*groups[item_id].values())
        evidence_ids[item_id] = [
            key
            for key in evidence_ids.get(item_id, [])
            if state["evidence_chunks"][key].generation_id in allowed
        ]
    return {
        **state,
        "plan": plan,
        "fact_values": {
            fact.fact_id: values[fact.fact_id] for fact in plan.facts if fact.fact_id in values
        },
        "clarification": clarification,
        "reply_message_ids": [*state["reply_message_ids"], reply_message_id],
        "tool_sessions": {
            key: value for key, value in state["tool_sessions"].items() if key not in changed
        },
        "chunk_history_by_item": {
            key: value
            for key, value in state["chunk_history_by_item"].items()
            if key not in changed
        },
        "decision": None,
        "pending_tool_executions": [],
        "search_queries": [
            query for query in state["search_queries"] if query.item_id not in changed
        ],
        "search_batches": batches,
        "ranked_chunk_ids": {
            key: value for key, value in state["ranked_chunk_ids"].items() if key not in changed
        },
        "evidence_ids_by_item": evidence_ids,
        "retrieval_notes": {
            key: value for key, value in state["retrieval_notes"].items() if key not in changed
        },
        "assessments": [value for value in state["assessments"] if value.item_id not in changed],
        "answer_sections": [
            value for value in state["answer_sections"] if value.item_id not in changed
        ],
        "result": None,
    }
