"""Process configuration boundary; components receive already parsed settings."""

import os
from pathlib import Path
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from intelligent_reimbursement_assitant.content_index.domain.config import IndexProfileSettings
from intelligent_reimbursement_assitant.dispatcher.domain.config import MaintenanceSettings
from intelligent_reimbursement_assitant.document_ingestion_service.domain.config import ApiSettings
from intelligent_reimbursement_assitant.execution_state_service.domain.models import ExecutionKind
from intelligent_reimbursement_assitant.ingestion_executor.domain.config import IngestionSettings
from intelligent_reimbursement_assitant.knowledge_wiki.domain.config import WikiSettings
from intelligent_reimbursement_assitant.policy_qa_service.domain.config import QASettings
from intelligent_reimbursement_assitant.worker_runtime.domain.config import QueueBinding

type Role = Literal["maintenance", "ingestion", "api", "migration", "qa", "wiki"]


class QueueGroupsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_group: str = Field(default="document_workers", min_length=1)
    agent_group: str = Field(default="agent_workers", min_length=1)
    wiki_group: str = Field(default="wiki_workers", min_length=1)


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    database_url: SecretStr = Field(min_length=1)
    redis_url: SecretStr | None = Field(default=None, min_length=1)
    managed_directory: Path | None = None
    queue_groups: QueueGroupsSettings = Field(default_factory=QueueGroupsSettings)
    maintenance: MaintenanceSettings = Field(default_factory=MaintenanceSettings)
    ingestion: IngestionSettings | None = None
    api: ApiSettings | None = None
    qa: QASettings | None = None
    wiki: WikiSettings | None = None
    index_profiles: dict[str, IndexProfileSettings] = Field(default_factory=dict)

    @model_validator(mode="after")
    def unique_profiles(self) -> AppSettings:
        physical_indexes = set()
        for profile_id, profile in self.index_profiles.items():
            if not profile_id.strip():
                raise ValueError("Index profile identifiers must not be empty")
            physical = (str(profile.elasticsearch.url), profile.elasticsearch.index_name)
            if physical in physical_indexes:
                raise ValueError("Different profiles must use different physical indexes")
            physical_indexes.add(physical)
        return self

    @field_validator("managed_directory")
    @classmethod
    def absolute_directory(cls, value: Path | None) -> Path | None:
        if value is not None and not value.is_absolute():
            raise ValueError("An absolute path is required")
        return value


class ConfigurationError(ValueError):
    """Contains only trusted role, schema field names and stable error categories."""

    def __init__(self, role: Role, issues: list[tuple[str, str]]):
        self.role = role
        self.issues = tuple(issues)
        super().__init__(f"Configuration {role}: " + "; ".join(f"{p}: {t}" for p, t in issues))


_STREAMS = {
    ExecutionKind.INGESTION: "document_jobs",
    ExecutionKind.AGENT_TURN: "qa_runs",
    ExecutionKind.WIKI_BUILD: "wiki_builds",
}
_GROUP_FIELDS = {
    ExecutionKind.INGESTION: "document_group",
    ExecutionKind.AGENT_TURN: "agent_group",
    ExecutionKind.WIKI_BUILD: "wiki_group",
}
_REQUIRED: dict[Role, tuple[str, ...]] = {
    "maintenance": ("redis_url",),
    "wiki": ("redis_url", "managed_directory", "wiki"),
    "ingestion": ("redis_url", "managed_directory", "ingestion"),
    "api": ("managed_directory", "api", "qa", "wiki"),
    "migration": (),
    "qa": ("qa", "redis_url", "managed_directory"),
}
# A profile ID or unknown extra key can itself contain a secret. Only schema-owned
# names are allowed in public locations, including when a value fails deep validation.
_SCHEMA = AppSettings.model_json_schema()
_FIELD_NAMES = frozenset(
    name
    for schema in (_SCHEMA, *_SCHEMA.get("$defs", {}).values())
    for name in schema.get("properties", {})
)


def load_settings(role: Role) -> AppSettings:
    raw = os.environ.get("APP_SETTINGS")
    if raw is None or not raw.strip():
        raise ConfigurationError(role, [("APP_SETTINGS", "missing")])
    issues = []
    try:
        settings = AppSettings.model_validate_json(raw)
    except ValidationError as error:
        for issue in error.errors(include_url=False, include_context=False, include_input=False):
            path = ".".join(str(part) if part in _FIELD_NAMES else "<key>" for part in issue["loc"])
            issues.append((path or "APP_SETTINGS", issue["type"]))
    # Raise outside the handler so no raw Pydantic input survives in the exception chain.
    if issues:
        raise ConfigurationError(role, issues)
    missing = [(name, "missing") for name in _REQUIRED[role] if getattr(settings, name) is None]
    if role in ("qa", "ingestion") and not settings.index_profiles:
        missing.append(("index_profiles", "missing"))
    if role == "api" and settings.api is not None:
        if settings.api.default_index_profile_id not in settings.index_profiles:
            missing.append(("api.default_index_profile_id", "profile_reference"))
    if role == "qa" and settings.qa is not None:
        missing.extend(
            (f"qa.{name}", "missing")
            for name in ("model", "business_timezone", "reranker")
            if getattr(settings.qa, name) is None
        )
        reranker = settings.qa.reranker
        if reranker is not None and settings.index_profiles:
            profile = settings.index_profiles.get(reranker.api_key_profile_id)
            if (
                profile is None
                or profile.embedding.api_key is None
                or not profile.embedding.api_key.get_secret_value().strip()
            ):
                missing.append(("qa.reranker.api_key_profile_id", "credential_reference"))
    if missing:
        raise ConfigurationError(role, missing)
    return settings


def queue_binding(groups: QueueGroupsSettings, kind: ExecutionKind) -> QueueBinding:
    return QueueBinding(
        kind=kind, stream=_STREAMS[kind], group=getattr(groups, _GROUP_FIELDS[kind])
    )
