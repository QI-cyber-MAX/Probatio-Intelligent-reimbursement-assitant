"""Assemble adopted claims and one complete source per cited chunk."""

from collections.abc import Mapping

from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    Chunk,
    KnowledgeSnapshot,
    VersionStatus,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.answering import (
    build_unsupported_section,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.models import (
    AnswerItem,
    AnswerResult,
    AnswerSections,
    Citation,
    EvidenceAssessment,
    TaskPlan,
)


def assemble_answer_result(
    plan: TaskPlan,
    answer_sections: AnswerSections,
    assessments: EvidenceAssessment,
    evidence_chunks: Mapping[str, Chunk],
    snapshot: KnowledgeSnapshot,
) -> AnswerResult:
    sections = {section.item_id: section for section in answer_sections}
    by_id = {assessment.item_id: assessment for assessment in assessments}
    citations: dict[str, Citation] = {}
    items = []
    used_facts: set[str] = set()
    for item in plan.items:
        assessment = by_id[item.item_id]
        section = (
            sections[item.item_id]
            if assessment.claims
            else build_unsupported_section(item, assessment)
        )
        claims_by_id = {claim.claim_id: claim for claim in assessment.claims}
        claims = [claims_by_id[key] for key in section.claim_ids]
        chunk_ids = dict.fromkeys(
            key for source in [*claims, *assessment.gaps] for key in source.evidence_chunk_ids
        )
        numbers = []
        for key in chunk_ids:
            if key not in citations:
                chunk = evidence_chunks[key]
                citations[key] = Citation(
                    number=len(citations) + 1,
                    chunk_id=key,
                    source_text=chunk.body_text,
                    locators=chunk.locators,
                )
            numbers.append(citations[key].number)
        notes = []
        historical = {}
        for key in chunk_ids:
            chunk = evidence_chunks[key]
            entry = next(e for e in snapshot.entries if e.generation_id == chunk.generation_id)
            if entry.policy_metadata.version_status == VersionStatus.HISTORICAL:
                historical.setdefault(entry.source, (entry.policy_metadata, []))[1].append(
                    citations[key].number
                )
        for policy, refs in historical.values():
            start = policy.effective_from
            end = policy.effective_to
            start_text = (
                f"{'登记' if start.source == 'USER' else '原文提取'}生效日期为{start.value}"
                if start
                else "生效日期未填写"
            )
            end_text = f"截止日期为{end.value}（不含当日）" if end else "截止日期未填写"
            labels = "、".join(f"[{n}]" for n in refs)
            notes.append(
                f"引用{labels}来自历史版本，{start_text}，{end_text}；本次适用还需结合费用发生时间及原文过渡条款。"
            )
        text = section.text + ("\n\n" + "\n".join(notes) if notes else "")
        used_facts.update(key for claim in claims for key in claim.fact_ids)
        items.append(
            AnswerItem(
                item_id=item.item_id,
                question=item.question,
                coverage=assessment.coverage,
                text=text,
                claims=claims,
                gaps=assessment.gaps,
                citation_numbers=numbers,
            )
        )
    return AnswerResult(
        items=items,
        facts=[fact for fact in plan.facts if fact.fact_id in used_facts],
        citations=list(citations.values()),
    )
