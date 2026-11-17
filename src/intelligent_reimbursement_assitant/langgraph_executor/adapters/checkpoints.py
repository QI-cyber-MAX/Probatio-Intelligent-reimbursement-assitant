"""Native Postgres Saver with the lease fence in the same physical transaction."""

from collections.abc import Sequence
from typing import Any, cast
from uuid import UUID

from langchain_core.runnables import RunnableConfig
from langgraph._internal._constants import NULL_TASK_ID
from langgraph.checkpoint.base import ChannelVersions, Checkpoint, CheckpointMetadata
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from psycopg import AsyncConnection, Connection
from psycopg import Error as PsycopgError
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from pydantic import TypeAdapter
from sqlalchemy import Engine

from intelligent_reimbursement_assitant.contracts.models import (
    ArtifactRef,
    SourceLocator,
    SourceReference,
)
from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    Chunk,
    KnowledgeSnapshot,
    PolicyMetadata,
    SnapshotEntry,
)
from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionContext,
    ExecutionStatus,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.ownership import (
    require_execution_owner,
)
from intelligent_reimbursement_assitant.langgraph_executor.ports.recovery import QARecovery
from intelligent_reimbursement_assitant.policy_qa_graph.application.graph import build_qa_graph
from intelligent_reimbursement_assitant.policy_qa_graph.domain.state import (
    QAState,
    restore_tool_admissions,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.models import (
    AnswerItem,
    AnswerResult,
    AnswerSection,
    AssessDecision,
    Citation,
    ClarificationRequest,
    DateFactValue,
    EvidenceGap,
    ItemAssessment,
    NumberFactValue,
    QAInput,
    ReadDecision,
    SearchBatch,
    SearchDecision,
    SearchQuery,
    SupportedClaim,
    TaskItem,
    TaskPlan,
    UserFact,
    WikiReadDecision,
)


def checkpoint_serializer() -> JsonPlusSerializer:
    return JsonPlusSerializer(
        allowed_msgpack_modules=[
            ArtifactRef,
            SourceReference,
            SourceLocator,
            Chunk,
            KnowledgeSnapshot,
            PolicyMetadata,
            SnapshotEntry,
            QAInput,
            UserFact,
            NumberFactValue,
            DateFactValue,
            SupportedClaim,
            EvidenceGap,
            Citation,
            AnswerItem,
            AnswerResult,
            ClarificationRequest,
            TaskItem,
            TaskPlan,
            SearchQuery,
            SearchDecision,
            ReadDecision,
            WikiReadDecision,
            AssessDecision,
            SearchBatch,
            ItemAssessment,
            AnswerSection,
        ]
    )


class FencedPostgresSaver(AsyncPostgresSaver):
    def __init__(
        self, pool: AsyncConnectionPool[AsyncConnection[dict[str, Any]]], ctx: ExecutionContext
    ):
        super().__init__(pool, serde=checkpoint_serializer())
        self.pool = pool
        self.ctx = ctx

    async def _fence(self, conn: AsyncConnection[dict[str, Any]], config: RunnableConfig) -> None:
        if config.get("configurable", {})["thread_id"] != str(self.ctx.job_id):
            raise ExecutionError(
                ErrorCode.EXECUTION_LEASE_LOST, "ABORT", "Checkpoint thread differs from execution"
            )
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM execution_run WHERE job_id=%s FOR UPDATE", (self.ctx.job_id,)
            )
            run = await cur.fetchone()
            await cur.execute("SELECT clock_timestamp() AS now")
            now = cast(dict[str, Any], await cur.fetchone())["now"]
        require_execution_owner(run, self.ctx, now)
        run = cast(dict[str, Any], run)
        if run["status"] == ExecutionStatus.CANCELLING:
            raise ExecutionError(
                ErrorCode.EXECUTION_CANCEL_REQUESTED, "ABORT", "Cancellation requested"
            )

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        attempted = False
        try:
            async with self.pool.connection() as conn:
                async with conn.transaction():
                    attempted = True
                    result = await AsyncPostgresSaver(conn, serde=self.serde).aput(
                        config, checkpoint, metadata, new_versions
                    )
                    # Native serialization uses the default thread pool. Do not hold
                    # the execution row while those threads may be waiting for it.
                    # The fence still precedes COMMIT in this physical transaction.
                    await self._fence(conn, config)
                return result
        except PsycopgError:
            if attempted:
                lookup: RunnableConfig = {
                    "configurable": {
                        **config.get("configurable", {}),
                        "checkpoint_id": checkpoint["id"],
                    }
                }
                if await self.aget_tuple(lookup) is not None:
                    return lookup
            # Absence cannot prove that the failed transaction rolled back.
            raise

    async def aput_writes(
        self,
        config: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        attempted = False
        try:
            async with self.pool.connection() as conn:
                async with conn.transaction():
                    attempted = True
                    await AsyncPostgresSaver(conn, serde=self.serde).aput_writes(
                        config, writes, task_id, task_path
                    )
                    await self._fence(conn, config)
        except PsycopgError:
            if attempted:
                saved = await self.aget_tuple(config)
                if saved is not None and all(
                    (task_id, channel, value) in (saved.pending_writes or [])
                    for channel, value in writes
                ):
                    return
            raise


_MODEL_NODES = frozenset({"understand", "retrieve", "assess", "clarify", "apply_reply", "answer"})
_STATE = TypeAdapter(QAState)


class PostgresQARecoveryReader:
    def __init__(self, engine: Engine):
        self.engine = engine

    def read(self, job_id: UUID) -> QARecovery:
        with self.engine.connect() as connection:
            native = cast(Connection[dict[str, Any]], connection.connection.driver_connection)
            saver = PostgresSaver(native, serde=checkpoint_serializer())
            graph = build_qa_graph(saver)
            config: RunnableConfig = {"configurable": {"thread_id": str(job_id)}}
            checkpoint = saver.get_tuple(config)
            snapshot = graph.get_state(config)
            orphan_writes = (
                checkpoint is None
                and connection.exec_driver_sql(
                    "SELECT EXISTS (SELECT 1 FROM checkpoint_writes WHERE thread_id=%s)",
                    (str(job_id),),
                ).scalar_one()
            )
        values = snapshot.values
        state = (
            _STATE.validate_python(restore_tool_admissions(values)) if "input" in values else None
        )
        writes = checkpoint.pending_writes or [] if checkpoint is not None else []
        resumed_tasks = {
            task_id for task_id, channel, value in writes if channel == "__resume__" and value
        }
        # 1.2.19 saves mapped replies as task-local resume lists; map commands
        # themselves remain invocation configuration, not a global resume map.
        resume_ids = frozenset(
            pending.id
            for task in snapshot.tasks
            for pending in task.interrupts
            if task.id in resumed_tasks or NULL_TASK_ID in resumed_tasks
        )
        if checkpoint is None:
            safe = not orphan_writes
        else:
            safe = checkpoint.metadata.get("source") == "input" or not any(
                name in _MODEL_NODES for name in snapshot.next
            )
            # A completed retrieval step can still own in-flight proposals in its
            # process-local generator. Only its final AssessDecision proves closure.
            if any(task.name == "retrieve" for task in snapshot.tasks) and (
                state is None or not isinstance(state["decision"], AssessDecision)
            ):
                safe = False
            if snapshot.interrupts:
                safe = True
        return QARecovery(checkpoint, snapshot, state, safe, resume_ids)
