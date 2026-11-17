"""Model-backed judgments over complete policy sources."""

from collections.abc import Callable, Mapping
from functools import partial

from pydantic import JsonValue

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
from intelligent_reimbursement_assitant.model_gateway.domain.config import ModelSettings
from intelligent_reimbursement_assitant.model_gateway.ports.model_gateway import ModelGateway
from intelligent_reimbursement_assitant.policy_qa_service.application.model_call import (
    call_structured,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.config import (
    QASettings,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.evidence import (
    assemble_item_assessment,
    build_evidence_context,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.model_responses import (
    AssessmentResponse,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.models import (
    EvidenceCheckRequest,
    EvidenceGap,
    FactValue,
    ItemAssessment,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.prompts import (
    COMMON,
    EVIDENCE,
    encode,
)
from intelligent_reimbursement_assitant.policy_qa_service.ports.repository import QARepository


class ModelEvidenceJudge:
    def __init__(
        self,
        *,
        gateway: ModelGateway,
        repository: QARepository,
        state_service: ExecutionStateService,
        settings: QASettings,
        model_settings: ModelSettings,
        count_tokens: Callable[[str], int],
        progress: ProgressFactory | None = None,
    ):
        self.gateway = gateway
        self.repository = repository
        self.state_service = state_service
        self.settings = settings
        self.model_settings = model_settings
        self.count_tokens = count_tokens
        self.progress = progress

    async def assess(
        self,
        request: EvidenceCheckRequest,
        ctx: ExecutionContext,
        *,
        fact_values: Mapping[str, FactValue],
    ) -> ItemAssessment:
        if not request.chunks:
            if self.progress is not None:
                await self.progress(ctx, request.item.item_id, None)(
                    ProgressStage.ASSESSING_EVIDENCE
                )
            return ItemAssessment(
                item_id=request.item.item_id,
                coverage="UNSUPPORTED",
                claims=[],
                gaps=[
                    EvidenceGap(
                        kind="POLICY_MISSING",
                        evidence_chunk_ids=[],
                        description=(
                            f"当前固定快照没有与“{request.item.question}”时段相交的制度原件。"
                            if not request.entries
                            else f"尚未读到支持“{request.item.question}”的完整制度正文。"
                        ),
                    )
                ],
            )
        context, chunk_bindings = build_evidence_context(request, fact_values)
        messages: list[dict[str, JsonValue]] = [
            {"role": "system", "content": COMMON + EVIDENCE},
            {"role": "user", "content": encode(context)},
        ]
        return await call_structured(
            messages=messages,
            response_type=AssessmentResponse,
            assemble=lambda response: assemble_item_assessment(
                request, response, fact_values, chunk_bindings
            ),
            gateway=self.gateway,
            repository=self.repository,
            state_service=self.state_service,
            ctx=ctx,
            settings=self.model_settings,
            count_tokens=self.count_tokens,
            structure_repair_limit=self.settings.structure_repair_limit,
            before_call=partial(
                self.progress(ctx, request.item.item_id, None), ProgressStage.ASSESSING_EVIDENCE
            )
            if self.progress
            else None,
        )
