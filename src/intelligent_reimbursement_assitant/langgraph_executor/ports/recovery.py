"""Read-only native recovery boundary shared by execution and maintenance."""

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from langgraph.checkpoint.base import CheckpointTuple
from langgraph.types import StateSnapshot

from intelligent_reimbursement_assitant.policy_qa_graph.domain.state import QAState


@dataclass(frozen=True)
class QARecovery:
    checkpoint: CheckpointTuple | None
    snapshot: StateSnapshot
    state: QAState | None
    safe: bool
    resume_ids: frozenset[str]


class QARecoveryReader(Protocol):
    def read(self, job_id: UUID) -> QARecovery: ...
