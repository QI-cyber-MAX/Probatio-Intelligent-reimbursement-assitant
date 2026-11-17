from typing import Protocol

from pydantic import JsonValue

from intelligent_reimbursement_assitant.contracts.models import ModelUsage


class ModelGateway(Protocol):
    async def decide_tools(
        self, messages: list[dict[str, JsonValue]], tools: list[dict[str, JsonValue]]
    ) -> tuple[dict[str, JsonValue], ModelUsage]: ...

    async def generate_structured(
        self, messages: list[dict[str, JsonValue]], response_schema: dict[str, JsonValue]
    ) -> tuple[dict[str, JsonValue], ModelUsage]: ...
