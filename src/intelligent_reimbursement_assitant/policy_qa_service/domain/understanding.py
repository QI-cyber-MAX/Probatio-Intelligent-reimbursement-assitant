"""Source-bound user facts, saved interpretations and task assembly."""

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.model_gateway.domain.model_text import share_texts
from intelligent_reimbursement_assitant.policy_qa_service.domain.model_inputs import (
    bind,
    fact_context,
    select,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.model_responses import (
    ClarificationUpdateResponse,
    DateFactResponse,
    FactsResponse,
    NumberFactResponse,
    PlanResponse,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.models import (
    DateFactValue,
    FactValue,
    NumberFactValue,
    TaskItem,
    TaskPlan,
    UserFact,
)


def assemble_user_facts(
    source_message_id: UUID,
    response: FactsResponse,
) -> tuple[list[UserFact], dict[str, FactValue]]:
    facts = []
    values: dict[str, FactValue] = {}
    for index, fact in enumerate(response.facts):
        identifier = str(uuid4())
        unit = None
        if isinstance(fact, NumberFactResponse):
            unit = fact.unit
            values[identifier] = NumberFactValue(value=Decimal(fact.value))
        elif isinstance(fact, DateFactResponse):
            try:
                values[identifier] = DateFactValue(
                    date_from=date.fromisoformat(fact.date_from),
                    date_to=date.fromisoformat(fact.date_to),
                )
            except ValueError as error:
                raise ValueError(f"facts[{index}].date_from/date_to: {error}") from error
        facts.append(
            UserFact(
                fact_id=identifier,
                meaning=fact.meaning,
                kind=fact.kind,
                unit=unit,
                source_message_id=source_message_id,
            )
        )
    return facts, values


def build_date_scopes(
    facts: list[UserFact], fact_values: Mapping[str, FactValue]
) -> dict[str, tuple[date, date]]:
    return {
        fact.fact_id: (value.date_from, value.date_to)
        for fact in facts
        if fact.kind == "date"
        for value in [cast(DateFactValue, fact_values[fact.fact_id])]
    }


def assemble_task_plan(
    response: PlanResponse, facts: list[UserFact], scopes: dict[str, tuple[date, date]]
) -> TaskPlan:
    if response.parse_issue is not None:
        code, message = {
            "privacy_request": (
                ErrorCode.QA_TASK_UNPARSABLE,
                "当前问题不可解析，涉及个人隐私数据请求",
            ),
            "prohibited_request": (
                ErrorCode.QA_TASK_UNPARSABLE,
                "当前问题不可解析，涉及违规行为请求",
            ),
            "unsupported_expression": (
                ErrorCode.QA_PARSE_UNSUPPORTED,
                "必要表达、单位或计算超出当前能力，无法可靠完成判断",
            ),
        }[response.parse_issue]
        raise ExecutionError(code, "PERMANENT", message)
    bindings = bind("F", facts)
    resolved = []
    for item in response.items:
        selected = [fact.fact_id for fact in select(item.fact_refs, bindings, "fact_refs")]
        date_fact = bindings.get(item.date_fact_ref) if item.date_fact_ref is not None else None
        if item.date_fact_ref is not None and (
            date_fact is None or date_fact.kind != "date" or date_fact.fact_id not in selected
        ):
            raise ValueError("date_fact_ref must select a date fact associated with this item")
        date_scope = date_fact.fact_id if date_fact is not None else None
        resolved.append((item, selected, date_scope))
        if len(item.missing_user_facts) != len(set(item.missing_user_facts)):
            raise ValueError("items.missing_user_facts must not contain duplicates")
    items = []
    used = set()
    for item, selected, date_scope in resolved:
        start, end = scopes[date_scope] if date_scope is not None else (None, None)
        items.append(
            TaskItem(
                item_id=str(uuid4()),
                question=item.question,
                kind=item.kind,
                date_from=start,
                date_to=end,
                fact_ids=selected,
            )
        )
        used.update(selected)
    return TaskPlan(
        facts=[fact for fact in facts if fact.fact_id in used], items=items, clarification=None
    )


def select_clarification(
    plan: TaskPlan, response: PlanResponse, clarification_count: int, clarification_limit: int
) -> tuple[list[str], list[str]]:
    # Missing fields are semantic output: the plan prompt excludes facts already refused
    # or unknown to the user and fields whose necessity first requires reading policy.
    if clarification_count >= clarification_limit or any(
        item.kind == "general_rule" or not projected.missing_user_facts
        for item, projected in zip(plan.items, response.items, strict=True)
    ):
        return [], []
    missing = list(
        dict.fromkeys(value for item in response.items for value in item.missing_user_facts)
    )
    return [item.item_id for item in plan.items], missing


def build_fact_references(plan: TaskPlan) -> dict[str, set[str]]:
    references: dict[str, set[str]] = {}
    for item in plan.items:
        for fact_id in item.fact_ids:
            references.setdefault(fact_id, set()).add(item.item_id)
    return references


def apply_clarification_update(
    old_plan: TaskPlan,
    response: ClarificationUpdateResponse,
    new_facts: list[UserFact],
    date_scopes: dict[str, tuple[date, date]],
    target_item_ids: set[str],
    fact_references: dict[str, set[str]],
) -> tuple[TaskPlan, set[str]]:
    old_facts = {fact.fact_id for fact in old_plan.facts}
    facts = old_plan.facts + new_facts
    fact_bindings = bind("F", facts)
    item_bindings = bind("I", old_plan.items)
    replaced = {
        fact.fact_id
        for fact in select(
            response.shared_replaced_fact_refs, fact_bindings, "shared_replaced_fact_refs"
        )
    }
    select([update.item_ref for update in response.updates], item_bindings, "updates.item_ref")
    updates = {item_bindings[update.item_ref].item_id: update for update in response.updates}
    if not replaced <= old_facts:
        raise ValueError("shared_replaced_fact_refs must select old facts")
    required = target_item_ids.union(*(fact_references[key] for key in replaced))
    if not required <= updates.keys():
        raise ValueError("updates must cover targets and every reference of replaced shared facts")
    for update in response.updates:
        selected_ids = [
            fact.fact_id for fact in select(update.fact_refs, fact_bindings, "fact_refs")
        ]
        selected = set(selected_ids)
        date_fact = (
            fact_bindings.get(update.date_fact_ref) if update.date_fact_ref is not None else None
        )
        date_scope = date_fact.fact_id if date_fact is not None else None
        if selected.intersection(replaced):
            raise ValueError("fact_refs must exclude shared replacements")
        if update.date_fact_ref is not None and (
            date_fact is None or date_fact.kind != "date" or date_scope not in selected
        ):
            raise ValueError("date_fact_ref must select a valid associated date fact")
        if len(update.missing_user_facts) != len(set(update.missing_user_facts)):
            raise ValueError("missing_user_facts must not contain duplicates")
    changed: set[str] = set()
    items = []
    for item in old_plan.items:
        update = updates.get(item.item_id)
        if update is not None:
            selected_ids = [fact_bindings[ref].fact_id for ref in update.fact_refs]
            date_scope = (
                fact_bindings[update.date_fact_ref].fact_id
                if update.date_fact_ref is not None
                else None
            )
            start, end = date_scopes[date_scope] if date_scope is not None else (None, None)
            if set(item.fact_ids) != set(selected_ids) or (item.date_from, item.date_to) != (
                start,
                end,
            ):
                changed.add(item.item_id)
                item = item.model_copy(
                    update={"fact_ids": selected_ids, "date_from": start, "date_to": end}
                )
        items.append(item)
    used = {key for item in items for key in item.fact_ids}
    return TaskPlan(
        facts=[f for f in facts if f.fact_id in used], items=items, clarification=None
    ), changed


def fact_extraction_input(
    messages: list[dict], target_index: int, reference_time: datetime, business_timezone: ZoneInfo
) -> dict:
    projected = [dict(message) for message in messages]
    projected[target_index]["target"] = True
    return share_texts(
        {
            "messages": projected,
            "reference_time": reference_time.isoformat(),
            "business_timezone": str(business_timezone),
        }
    )


def plan_input(
    messages: list[dict],
    facts: list[UserFact],
    sources: dict[UUID, int],
    fact_values: Mapping[str, FactValue],
    *,
    wiki_overview: str | None = None,
) -> dict:
    return share_texts(
        {
            "messages": messages,
            "facts": {
                ref: {
                    **fact_context(fact, fact_values),
                    "source": sources[fact.source_message_id],
                }
                for ref, fact in bind("F", facts).items()
            },
            **({"wiki_overview": wiki_overview} if wiki_overview else {}),
        }
    )


def item_context(
    item: TaskItem, facts: list[UserFact], fact_values: Mapping[str, FactValue]
) -> dict:
    return {
        "question": item.question,
        "context": [
            fact_context(fact, fact_values) for fact in facts if fact.fact_id in item.fact_ids
        ],
    }
