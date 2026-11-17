"""Shared source context, evidence claim bindings and exact Decimal comparisons."""

from collections.abc import Mapping
from decimal import Decimal, localcontext
from typing import Any, cast
from uuid import uuid4

from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    Chunk,
    KnowledgeSnapshot,
    SnapshotEntry,
)
from intelligent_reimbursement_assitant.document_ingestion_service.domain.policy_metadata import (
    date_bounds,
    overlaps_policy,
)
from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.model_gateway.domain.model_text import (
    prepare_model_text,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.model_inputs import (
    bind,
    fact_context,
    period_context,
    select,
    share_context_texts,
    task_context,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.model_responses import (
    AssessmentResponse,
    EvidenceClarificationResponse,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.models import (
    ClarificationRequest,
    Coverage,
    EvidenceAssessment,
    EvidenceCheckRequest,
    EvidenceGap,
    FactValue,
    ItemAssessment,
    NumberFactValue,
    SupportedClaim,
    TaskItem,
    TaskPlan,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.tool_calling import (
    build_policy_catalog,
    chunk_heading,
    select_snapshot_entries,
)


def select_assessment_items(plan: TaskPlan, assessments: EvidenceAssessment) -> list[TaskItem]:
    assessed = {assessment.item_id for assessment in assessments}
    return [item for item in plan.items if item.item_id not in assessed]


def build_evidence_request(
    item: TaskItem,
    plan: TaskPlan,
    snapshot: KnowledgeSnapshot,
    evidence_chunks: Mapping[str, Chunk],
    evidence_ids: list[str],
    retrieval_note: str | None = None,
) -> EvidenceCheckRequest:
    facts = set(item.fact_ids)
    return EvidenceCheckRequest(
        item=item,
        facts=[fact for fact in plan.facts if fact.fact_id in facts],
        entries=select_snapshot_entries(item, snapshot),
        chunks=[evidence_chunks[key] for key in evidence_ids],
        retrieval_note=retrieval_note,
    )


def build_time_segments(item: TaskItem, entries: list[SnapshotEntry]) -> list[dict[str, Any]]:
    if item.date_from is None or item.date_to is None:
        return [
            {
                "date_from": None,
                "date_to": None,
                "generation_ids": [str(entry.generation_id) for entry in entries],
            }
        ]
    boundaries = {item.date_from, item.date_to}
    for entry in entries:
        for boundary in (entry.policy_metadata.effective_from, entry.policy_metadata.effective_to):
            if boundary is not None:
                for day in date_bounds(boundary):
                    if item.date_from < day < item.date_to:
                        boundaries.add(day)
    ordered = sorted(boundaries)
    return [
        {
            "date_from": start.isoformat(),
            "date_to": end.isoformat(),
            "generation_ids": [
                str(entry.generation_id)
                for entry in entries
                if overlaps_policy(entry.policy_metadata, start, end)
            ],
        }
        for start, end in zip(ordered, ordered[1:], strict=False)
    ]


def build_source_context(request: EvidenceCheckRequest) -> tuple[dict, dict[str, str]]:
    chunk_bindings = {ref: chunk.chunk_id for ref, chunk in bind("C", request.chunks).items()}
    titles = [entry.title for entry in request.entries if entry.title is not None]
    bodies = {
        chunk.chunk_id: {
            **chunk_heading(chunk),
            "body": prepare_model_text(chunk.body_text, titles),
        }
        for chunk in request.chunks
    }
    documents_with_evidence = {chunk.document_id for chunk in request.chunks}
    entries = [
        entry for entry in request.entries if entry.source.document_id in documents_with_evidence
    ]
    policies, generation_refs, _ = build_policy_catalog(entries, {})
    generation_revisions = {str(key): ref for key, ref in generation_refs.items()}
    projected_chunks = {
        ref: {
            **bodies[key],
            "revision_ref": generation_refs[
                next(c for c in request.chunks if c.chunk_id == key).generation_id
            ],
        }
        for ref, key in chunk_bindings.items()
    }
    segments = [
        {
            "from": segment["date_from"],
            "to": segment["date_to"],
            "revisions": list(
                dict.fromkeys(generation_revisions[key] for key in segment["generation_ids"])
            ),
        }
        for segment in build_time_segments(request.item, entries)
    ]

    return {
        "task": task_context(request.item),
        "period": period_context(request.item),
        "policies": policies,
        "chunks": projected_chunks,
        "time_segments": segments,
    }, chunk_bindings


def build_evidence_context(
    request: EvidenceCheckRequest, fact_values: Mapping[str, FactValue]
) -> tuple[dict, dict[str, str]]:
    context, chunk_bindings = build_source_context(request)
    context["facts"] = {
        ref: fact_context(fact, fact_values) for ref, fact in bind("F", request.facts).items()
    }
    context["retrieval_note"] = request.retrieval_note
    return share_context_texts(context), chunk_bindings


def evaluate_comparison(left: Decimal, operator: str, right: Decimal) -> bool:
    return {
        "LE": left <= right,
        "LT": left < right,
        "GE": left >= right,
        "GT": left > right,
        "EQ": left == right,
    }[operator]


def finalize_coverage(
    coverage: Coverage, claims: list[SupportedClaim], gaps: list[EvidenceGap]
) -> Coverage:
    valid = {
        "COMPLETE": bool(claims) and not gaps,
        "PARTIAL": bool(claims) and bool(gaps),
        "UNSUPPORTED": not claims,
    }
    if not valid[coverage]:
        raise ValueError("coverage must agree with publishable claims and real gaps")
    return coverage


def assemble_item_assessment(
    request: EvidenceCheckRequest,
    response: AssessmentResponse,
    fact_values: Mapping[str, FactValue],
    chunk_bindings: Mapping[str, str],
) -> ItemAssessment:
    if response.parse_issue is not None:
        if response.coverage != "UNSUPPORTED" or response.claims or response.gaps:
            raise ValueError("parse_issue requires UNSUPPORTED and empty claims/gaps")
        issue = response.parse_issue
        raise ExecutionError(
            ErrorCode.QA_PARSE_UNSUPPORTED,
            "PERMANENT",
            f"parse_issue at {issue.location}: {issue.reason}",
        )
    fact_bindings = bind("F", request.facts)
    claims = []
    for index, proposed in enumerate(response.claims):
        path = f"claims[{index}]"
        facts = select(proposed.fact_refs, fact_bindings, f"{path}.fact_refs")
        chunk_ids = select(proposed.chunk_refs, chunk_bindings, f"{path}.chunk_refs")
        if not chunk_ids:
            raise ValueError(f"{path}.chunk_refs: every supported claim requires a source chunk")
        rule = SupportedClaim(
            claim_id=str(uuid4()),
            text=proposed.text,
            fact_ids=[fact.fact_id for fact in facts],
            evidence_chunk_ids=chunk_ids,
        )
        claims.append(rule)
        for comparison_index, comparison in enumerate(proposed.comparisons):
            field = f"{path}.comparisons[{comparison_index}]"
            fact = select([comparison.fact_ref], fact_bindings, f"{field}.fact_ref")[0]
            if fact.kind != "number":
                raise ValueError(f"{field}.fact_ref must select this item's number fact")
            literal = cast(NumberFactValue, fact_values[fact.fact_id])
            policy_value = Decimal(comparison.policy_value)
            factor = Decimal(comparison.policy_factor)
            if not policy_value.is_finite() or not factor.is_finite() or factor <= 0:
                raise ValueError(f"{field}: operands must be finite and policy_factor positive")
            with localcontext() as context:
                context.prec = max(
                    1, len(policy_value.as_tuple().digits) + len(factor.as_tuple().digits)
                )
                converted = policy_value * factor
            outcome = evaluate_comparison(literal.value, comparison.operator, converted)
            symbol = {"LE": "≤", "LT": "<", "GE": "≥", "GT": ">", "EQ": "="}[comparison.operator]
            claims.append(
                SupportedClaim(
                    claim_id=str(uuid4()),
                    text=(
                        f"适用规则：{rule.text}；{fact.meaning}：{literal.value} {fact.unit}；"
                        f"政策数值：{comparison.policy_value} {comparison.policy_unit}；"
                        f"倍率：{comparison.policy_factor}；换算后：{converted} {fact.unit}；"
                        f"数值条件 {literal.value} {symbol} {converted} {fact.unit}"
                        f"（{'成立' if outcome else '不成立'}）。"
                    ),
                    fact_ids=list(dict.fromkeys([*rule.fact_ids, fact.fact_id])),
                    evidence_chunk_ids=rule.evidence_chunk_ids,
                )
            )
    gaps = [
        EvidenceGap(
            kind=gap.kind,
            description=gap.description,
            evidence_chunk_ids=select(gap.chunk_refs, chunk_bindings, f"gaps[{index}].chunk_refs"),
        )
        for index, gap in enumerate(response.gaps)
    ]
    return ItemAssessment(
        item_id=request.item.item_id,
        coverage=finalize_coverage(response.coverage, claims, gaps),
        claims=claims,
        gaps=gaps,
    )


def merge_assessments(
    plan: TaskPlan, old: EvidenceAssessment, new: EvidenceAssessment
) -> EvidenceAssessment:
    by_id = {assessment.item_id: assessment for assessment in [*old, *new]}
    return [by_id[item.item_id] for item in plan.items if item.item_id in by_id]


def clarification_options(
    plan: TaskPlan, assessments: EvidenceAssessment
) -> dict[str, tuple[str, str]]:
    by_id = {assessment.item_id: assessment for assessment in assessments}
    return bind(
        "G",
        (
            (item.item_id, gap.description)
            for item in plan.items
            for gap in by_id[item.item_id].gaps
            if gap.kind == "USER_FACT"
        ),
    )


def assemble_evidence_clarification(
    response: EvidenceClarificationResponse,
    plan: TaskPlan,
    options: Mapping[str, tuple[str, str]],
) -> ClarificationRequest | None:
    selected = set(response.gap_refs)
    if len(selected) != len(response.gap_refs) or not selected <= options.keys():
        raise ValueError("gap_refs must select unique USER_FACT options of this request")
    if bool(selected) != (response.question is not None):
        raise ValueError(
            "question must be nonempty for selected gaps and null for an empty selection"
        )
    if not selected:
        return None
    item_ids = {options[label][0] for label in selected}
    return ClarificationRequest(
        item_ids=[item.item_id for item in plan.items if item.item_id in item_ids],
        question=cast(str, response.question),
    )
