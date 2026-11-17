"""QA message schema and execution errors over shared native transport."""

import httpx
from pydantic import JsonValue

from intelligent_reimbursement_assitant.contracts.models import ModelUsage
from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.model_gateway.adapters.deepseek import (
    DeepSeekModelGateway as NativeDeepSeekModelGateway,
)
from intelligent_reimbursement_assitant.model_gateway.adapters.deepseek import (
    ModelErrorCodes,
)
from intelligent_reimbursement_assitant.model_gateway.domain.config import ModelSettings
from intelligent_reimbursement_assitant.policy_qa_service.domain.prompts import structured_messages


class DeepSeekModelGateway(NativeDeepSeekModelGateway):
    def __init__(self, client: httpx.AsyncClient, settings: ModelSettings):
        super().__init__(
            client,
            settings,
            ModelErrorCodes(
                configuration=ErrorCode.QA_MODEL_CONFIGURATION_ERROR,
                unavailable=ErrorCode.QA_MODEL_UNAVAILABLE,
                outcome_unknown=ErrorCode.QA_MODEL_OUTCOME_UNKNOWN,
            ),
        )

    async def generate_structured(
        self, messages: list[dict[str, JsonValue]], response_schema: dict[str, JsonValue]
    ) -> tuple[dict[str, JsonValue], ModelUsage]:
        return await super().generate_structured(
            structured_messages(messages, response_schema), response_schema
        )
