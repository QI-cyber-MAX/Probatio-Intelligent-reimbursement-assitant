"""Final prose generation from complete sources and ordered claim selection."""

from collections.abc import Mapping

from intelligent_reimbursement_assitant.policy_qa_service.domain.evidence import (
    build_source_context,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.model_inputs import (
    bind,
    fact_context,
    select,
    share_context_texts,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.model_responses import (
    AnswerSectionResponse,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.models import (
    AnswerSection,
    AnswerSections,
    EvidenceCheckRequest,
    FactValue,
    ItemAssessment,
    TaskItem,
    TaskPlan,
)


def select_answer_items(plan: TaskPlan, answer_sections: AnswerSections) -> list[TaskItem]:
    existing = {section.item_id for section in answer_sections}
    return [item for item in plan.items if item.item_id not in existing]


def assemble_answer_section(
    item: TaskItem, assessment: ItemAssessment, response: AnswerSectionResponse
) -> AnswerSection:
    claims = select(response.claim_refs, bind("A", assessment.claims), "claim_refs")
    if assessment.claims and not claims:
        raise ValueError("claim_refs must select at least one available claim")
    return AnswerSection(
        item_id=item.item_id, text=response.text, claim_ids=[claim.claim_id for claim in claims]
    )


def build_unsupported_section(item: TaskItem, assessment: ItemAssessment) -> AnswerSection:
    labels = {
        "USER_FACT": "尚缺少用户信息",
        "POLICY_MISSING": "尚缺少制度依据",
        "CONFLICT": "尚未解决的制度冲突",
    }
    details = "；".join(f"{labels[gap.kind]}：{gap.description}" for gap in assessment.gaps)
    return AnswerSection(
        item_id=item.item_id,
        text=f"当前无法确认“{item.question}”。" + (f"{details}。" if details else ""),
        claim_ids=[],
    )


def merge_answer_sections(
    plan: TaskPlan, old: AnswerSections, new: AnswerSections
) -> AnswerSections:
    by_id = {section.item_id: section for section in [*old, *new]}
    return [by_id[item.item_id] for item in plan.items]


def build_answer_context(
    request: EvidenceCheckRequest,
    assessment: ItemAssessment,
    fact_values: Mapping[str, FactValue],
) -> dict:
    context, chunks = build_source_context(request)
    fact_bindings = bind("F", request.facts)
    facts = {fact.fact_id: ref for ref, fact in fact_bindings.items()}
    reverse_chunks = {key: ref for ref, key in chunks.items()}
    context["facts"] = {ref: fact_context(fact, fact_values) for ref, fact in fact_bindings.items()}
    context["claims"] = {
        ref: {
            "text": claim.text,
            "fact_refs": [facts[key] for key in claim.fact_ids],
            "chunk_refs": [reverse_chunks[key] for key in claim.evidence_chunk_ids],
        }
        for ref, claim in bind("A", assessment.claims).items()
    }
    context["gaps"] = [
        {
            "kind": gap.kind,
            "description": gap.description,
            "chunk_refs": [reverse_chunks[key] for key in gap.evidence_chunk_ids],
        }
        for gap in assessment.gaps
    ]
    return share_context_texts(context)
