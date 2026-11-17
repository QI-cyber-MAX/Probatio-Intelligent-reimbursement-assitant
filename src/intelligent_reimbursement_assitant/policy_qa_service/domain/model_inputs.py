"""Request-local references and lossless text views; business identities stay local."""

from collections.abc import Iterable, Mapping
from typing import Any

from intelligent_reimbursement_assitant.model_gateway.domain.model_text import share_texts
from intelligent_reimbursement_assitant.policy_qa_service.domain.models import (
    DateFactValue,
    FactValue,
    NumberFactValue,
    TaskItem,
    UserFact,
)


def bind[T](prefix: str, values: Iterable[T]) -> dict[str, T]:
    return {f"{prefix}{index}": value for index, value in enumerate(values, 1)}


def select[T](references: list[str], bindings: Mapping[str, T], field: str) -> list[T]:
    if len(references) != len(set(references)) or not set(references) <= bindings.keys():
        raise ValueError(f"{field} must select unique supplied references")
    return [bindings[reference] for reference in references]


def fact_context(fact: UserFact, fact_values: Mapping[str, FactValue]) -> dict:
    result: dict[str, object] = {"meaning": fact.meaning, "kind": fact.kind}
    if fact.kind in ("number", "date"):
        value = fact_values[fact.fact_id]
        if isinstance(value, NumberFactValue):
            result.update(value=format(value.value, "f"), unit=fact.unit)
        elif isinstance(value, DateFactValue):
            result.update(date_from=value.date_from.isoformat(), date_to=value.date_to.isoformat())
    return result


def task_context(item: TaskItem) -> dict:
    return {"question": item.question, "kind": item.kind}


def period_context(item: TaskItem) -> dict | None:
    return (
        {"from": item.date_from.isoformat(), "to": item.date_to.isoformat()}
        if item.date_from is not None and item.date_to is not None
        else None
    )


def share_context_texts(context: dict) -> dict:
    """Share prose fields while preserving protocol enums, values and references."""
    prose_fields = {
        "question",
        "meaning",
        "unit",
        "body",
        "heading",
        "content",
        "text",
        "description",
        "retrieval_note",
        "policy_gaps",
    }
    content = []

    def collect(value: Any, field: str = "") -> None:
        if isinstance(value, str) and field in prose_fields:
            content.append(value)
        elif isinstance(value, dict):
            for key, child in value.items():
                collect(child, key)
        elif isinstance(value, list):
            for child in value:
                collect(child, field)

    collect(context)
    shared = share_texts({"content": content})
    replacements = iter(shared["content"])

    def project(value: Any, field: str = "") -> Any:
        if isinstance(value, str) and field in prose_fields:
            return next(replacements)
        if isinstance(value, dict):
            return {key: project(child, key) for key, child in value.items()}
        if isinstance(value, list):
            return [project(child, field) for child in value]
        return value

    return {**project(context), **({"texts": shared["texts"]} if "texts" in shared else {})}
