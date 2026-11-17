"""Generate one section from current claims through the shared model boundary."""

from collections.abc import Callable, Mapping
from functools import partial

from pydantic import JsonValue

from intelligent_reimbursement_assitant.execution_state_service.application.service import (
    ExecutionStateService,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.events import (
    ProgressStage,
    ReportProgress,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionContext,
)
from intelligent_reimbursement_assitant.model_gateway.domain.config import ModelSettings
from intelligent_reimbursement_assitant.model_gateway.ports.model_gateway import ModelGateway
from intelligent_reimbursement_assitant.policy_qa_service.application.model_call import (
    call_structured,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.answering import (
    assemble_answer_section,
    build_answer_context,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.config import (
    QASettings,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.model_responses import (
    AnswerSectionResponse,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.models import (
    AnswerSection,
    EvidenceCheckRequest,
    FactValue,
    ItemAssessment,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.prompts import (
    ANSWER,
    COMMON,
    encode,
)
from intelligent_reimbursement_assitant.policy_qa_service.ports.repository import QARepository


async def generate_answer_section(
    request: EvidenceCheckRequest,
    assessment: ItemAssessment,
    ctx: ExecutionContext,
    *,
    fact_values: Mapping[str, FactValue],
    gateway: ModelGateway,
    repository: QARepository,
    state_service: ExecutionStateService,
    settings: QASettings,
    model_settings: ModelSettings,
    count_tokens: Callable[[str], int],
    report_progress: ReportProgress | None = None,
) -> AnswerSection:
    context = build_answer_context(request, assessment, fact_values)
    messages: list[dict[str, JsonValue]] = [
        {"role": "system", "content": COMMON + ANSWER},
        {"role": "user", "content": encode(context)},
    ]
    return await call_structured(
        messages=messages,
        response_type=AnswerSectionResponse,
        assemble=lambda response: assemble_answer_section(request.item, assessment, response),
        gateway=gateway,
        repository=repository,
        state_service=state_service,
        ctx=ctx,
        settings=model_settings,
        count_tokens=count_tokens,
        structure_repair_limit=settings.structure_repair_limit,
        before_call=partial(report_progress, ProgressStage.GENERATING_ANSWER)
        if report_progress
        else None,
    )
