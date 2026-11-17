"""Durable wiki compilation, with one execution-local state owner per build."""

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, cast

from pydantic import BaseModel, ValidationError
from sqlalchemy.exc import SQLAlchemyError

from intelligent_reimbursement_assitant.contracts.models import ArtifactRef
from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    SnapshotEntry,
)
from intelligent_reimbursement_assitant.errors.codes import ErrorCode as Code
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.errors.execution import (
    exception_diagnostic,
    gather_settled,
    retry_safe_operation,
    run_io,
)
from intelligent_reimbursement_assitant.execution_state_service.application.service import (
    ExecutionStateService,
)
from intelligent_reimbursement_assitant.execution_state_service.domain.models import (
    ExecutionContext,
    WorkflowSucceeded,
)
from intelligent_reimbursement_assitant.knowledge_wiki.domain.compilation import (
    WikiContext,
    bind_incremental_themes,
    bind_overview,
    bind_overview_update,
    bind_reference_rebinding,
    bind_statement,
    bind_statements,
    bind_theme_update,
    bind_themes,
    build_classification_context,
    build_context,
    build_incremental_classification_context,
    build_overview_context,
    build_overview_update_context,
    build_reference_rebinding_context,
    build_review_context,
    build_theme_update_context,
    chunk_content,
    document_page_id,
    encode,
    page_with_references,
    rebind_exact_references,
    select_reusable_pages,
    source_page_with_version_notice,
    source_version_notice,
)
from intelligent_reimbursement_assitant.knowledge_wiki.domain.config import WikiSettings
from intelligent_reimbursement_assitant.knowledge_wiki.domain.model_responses import (
    ChunkReadArguments,
    WikiAssistantMessage,
    WikiIncrementalClassificationResponse,
    WikiOverviewResponse,
    WikiOverviewUpdateResponse,
    WikiReadArguments,
    WikiReferenceRebindingResponse,
    WikiReviewResponse,
    WikiStatementsResponse,
    WikiThemeClassificationResponse,
    WikiThemeUpdateResponse,
)
from intelligent_reimbursement_assitant.knowledge_wiki.domain.models import (
    WikiChunkRef,
    WikiPackage,
    WikiPage,
    WikiStatement,
    WikiThemePlan,
)
from intelligent_reimbursement_assitant.knowledge_wiki.ports.artifacts import WikiBuildReaderPort
from intelligent_reimbursement_assitant.knowledge_wiki.ports.repository import WikiRepository
from intelligent_reimbursement_assitant.model_gateway.ports.model_gateway import ModelGateway
from intelligent_reimbursement_assitant.scanner.ports.artifact_store import ArtifactStorePort

logger = logging.getLogger(__name__)


class WikiExecutor:
    def __init__(
        self,
        repository: WikiRepository,
        state: ExecutionStateService,
        store: ArtifactStorePort,
        reader: WikiBuildReaderPort,
        gateway: ModelGateway,
        settings: WikiSettings,
        *,
        tool_pool: asyncio.Semaphore | None = None,
    ):
        self.repository, self.state, self.store, self.reader = repository, state, store, reader
        self.gateway, self.settings = gateway, settings
        self.tool_pool = tool_pool or asyncio.Semaphore(settings.tool_concurrency)
        self.model_pool = asyncio.Semaphore(settings.model_concurrency)
        # Admission by stage happens before the total pool, so queued generation
        # cannot occupy the slots reserved for reviewing completed candidates.
        stage_capacity = max(1, settings.model_concurrency // 2)
        self.generation_pool = asyncio.Semaphore(stage_capacity)
        self.review_pool = asyncio.Semaphore(stage_capacity)

    async def execute(self, ctx: ExecutionContext) -> WorkflowSucceeded:
        runner = _Compilation(self, ctx)
        task = asyncio.create_task(runner.run())
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            runner.stopping = True
            # Let the issued call settle its actual usage and reliable output.
            while not task.done():
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    continue
            task.result()
            raise


class _Compilation:
    def __init__(self, owner: WikiExecutor, ctx: ExecutionContext):
        self.owner, self.ctx = owner, ctx
        self.stopping = False
        self.checkpoint: dict = {}
        self.pages: dict[str, WikiPage] = {}
        self.reports = {}
        self.lock = asyncio.Lock()
        self.budget_exhausted = False
        self.failure: BaseException | None = None

    async def save(self, merge: Callable[[], None] = lambda: None) -> None:
        async with self.lock:
            merge()
            checkpoint = json.loads(encode(self.checkpoint))

            def write():
                with self.owner.repository.session() as session:
                    self.owner.repository.save_checkpoint(session, self.ctx, checkpoint)
                    session.commit()

            try:
                await asyncio.to_thread(write)
            except SQLAlchemyError as error:
                raise ExecutionError(
                    Code.WIKI_MODEL_OUTCOME_UNKNOWN,
                    "OUTCOME_UNKNOWN",
                    "Checkpoint commit outcome unknown",
                ) from error

    async def save_step(self, step: str, state: dict) -> None:
        await self.save(
            lambda: self.checkpoint.setdefault("steps", {}).update(
                {step: json.loads(encode(state))}
            )
        )

    async def save_page(self, step: str, page: WikiPage) -> WikiPage:
        def merge():
            self.pages[page.page_id] = page
            self.checkpoint.setdefault("pages", {})[page.page_id] = page.model_dump(mode="json")
            self.checkpoint.setdefault("steps", {}).pop(step, None)

        await self.save(merge)
        return page

    def stop(self, error: BaseException) -> None:
        self.stopping = True

        # Unknown side effects must not be hidden by an earlier safe failure or cancellation.
        def priority(error: BaseException) -> int:
            if isinstance(error, asyncio.CancelledError):
                return 0
            if isinstance(error, ExecutionError):
                return {
                    "LOCAL_RETRYABLE": 1,
                    "TASK_RETRYABLE": 1,
                    "ABORT": 2,
                    "PERMANENT": 3,
                    "OUTCOME_UNKNOWN": 4,
                }[error.category]
            return 4

        if self.failure is None or priority(error) > priority(self.failure):
            self.failure = error

    async def parallel[T](self, operations: list[Awaitable[T]]) -> list[T]:
        async def run(operation: Awaitable[T]) -> T:
            try:
                return await operation
            except BaseException as error:
                self.stop(error)
                raise

        # Never cancel siblings: issued calls must settle usage and commit reliable results.
        results = await asyncio.gather(*(run(op) for op in operations), return_exceptions=True)
        if self.failure is not None:
            raise self.failure
        return cast(list[T], results)

    async def eligible(self) -> bool:
        if self.stopping:
            raise asyncio.CancelledError()

        def check():
            with self.owner.repository.session() as session:
                allowed = self.owner.state.check_execution(session, self.ctx)
                if allowed:
                    session.commit()
                # Defer the terminal budget transition until all outstanding results are saved.
                else:
                    session.rollback()
                return allowed

        async with self.lock:
            if self.stopping:
                raise asyncio.CancelledError()
            allowed = await asyncio.to_thread(check)
            if not allowed:
                self.budget_exhausted = True
                self.stopping = True
            return allowed

    def step(self, key: str, stage: str) -> dict:
        return {"stage": stage, **json.loads(encode(self.checkpoint.get("steps", {}).get(key, {})))}

    async def request_model(
        self,
        context: WikiContext,
        stage: str,
        step: str,
        state: dict,
        messages: list,
        *,
        tools: list | None = None,
    ) -> dict:
        async def issue():
            state["model_call_started"] = True
            await self.save_step(step, state)
            if self.stopping:
                state["model_call_started"] = False
                await self.save_step(step, state)
                raise asyncio.CancelledError()
            try:
                raw, usage = (
                    await self.owner.gateway.decide_tools(messages, tools)
                    if tools is not None
                    else await self.owner.gateway.generate_structured(
                        messages, context.request["response_schema"]
                    )
                )
            except ExecutionError as error:
                if error.category != "LOCAL_RETRYABLE":
                    self.stop(error)
                if error.category in ("LOCAL_RETRYABLE", "PERMANENT"):
                    state["model_call_started"] = False
                    await self.save_step(step, state)
                raise
            if usage.input_tokens is None:
                raise ExecutionError(
                    Code.WIKI_MODEL_OUTCOME_UNKNOWN, "OUTCOME_UNKNOWN", "Model input usage unknown"
                )

            def account():
                with self.owner.repository.session() as session:
                    self.owner.state.record_model_usage(
                        session, self.ctx.job_id, cast(int, usage.input_tokens)
                    )
                    session.commit()

            try:
                async with self.lock:
                    await asyncio.to_thread(account)
            except SQLAlchemyError as error:
                raise ExecutionError(
                    Code.WIKI_MODEL_OUTCOME_UNKNOWN, "OUTCOME_UNKNOWN", "Model usage commit unknown"
                ) from error
            return raw

        async def request():
            async def admitted():
                async with self.owner.model_pool:
                    if not await self.eligible():
                        raise asyncio.CancelledError()
                    return await issue()

            pool = (
                self.owner.generation_pool
                if stage == "SOURCE_ANALYSIS"
                else self.owner.review_pool
                if stage == "CONTENT_REVIEW"
                else None
            )
            if pool is not None:
                async with pool:
                    return await admitted()
            return await admitted()

        raw = await retry_safe_operation(request, self.eligible)
        try:
            choices = raw["choices"]
            if (
                not isinstance(choices, list)
                or len(choices) != 1
                or not isinstance(choices[0], dict)
            ):
                raise ValueError("Invalid native response")
            choice = choices[0]
            finish = choice.get("finish_reason")
            if finish in {"length", "content_filter"}:
                failure = ExecutionError(
                    Code.WIKI_CONTENT_TOO_LARGE
                    if finish == "length"
                    else Code.WIKI_MODEL_INVALID_RESPONSE,
                    "PERMANENT",
                    f"Model output {finish} at {stage}/{step}",
                )
                self.stop(failure)
                state["model_call_started"] = False
                await self.save_step(step, state)
                raise failure
            if finish not in (("stop", "tool_calls") if tools is not None else ("stop",)):
                raise ExecutionError(
                    Code.WIKI_MODEL_OUTCOME_UNKNOWN, "OUTCOME_UNKNOWN", "Model output interrupted"
                )
            message = choice["message"]
            if not isinstance(message, dict):
                raise ValueError("Missing native message")
            if tools is None and not isinstance(message.get("content"), str):
                raise ValueError("Missing native content")
            if tools is not None:
                parsed = WikiAssistantMessage.model_validate(message)
                ids = [c.id for c in parsed.tool_calls]
                previous = {
                    c["id"]
                    for m in state["tool_session"]["messages"]
                    if m["role"] == "assistant"
                    for c in m.get("tool_calls", [])
                }
                if (
                    len(ids) != len(set(ids))
                    or previous.intersection(ids)
                    or (finish == "tool_calls" and not ids)
                ):
                    raise ValueError("Unassociable native tool calls")
                if not ids and parsed.content is None:
                    raise ValueError("Missing final content")
                message = parsed.model_dump(mode="json", exclude_unset=True)
            return message
        except (KeyError, ValueError, TypeError) as error:
            raise ExecutionError(
                Code.WIKI_MODEL_OUTCOME_UNKNOWN,
                "OUTCOME_UNKNOWN",
                "Native output cannot be confirmed",
            ) from error

    async def call_wiki_model(
        self,
        context: WikiContext,
        stage: str,
        step: str,
        response_type: type[BaseModel],
        validate: Callable,
        state: dict,
        *,
        content: str | None = None,
    ) -> object:
        repair = state.get("repair_input")
        if repair is not None:
            context = WikiContext.restored(repair)
            content = None
        while True:
            if content is None:
                repair_data = (
                    {"previous_output": repair["previous_output"], "errors": repair["errors"]}
                    if repair
                    else None
                )
                messages = context.messages(
                    stage, repair_data, state.get("tool_session", {}).get("messages")
                )
                message = await self.request_model(context, stage, step, state, messages)
                content = cast(str, message["content"])
            try:
                response = response_type.model_validate_json(content)
                result = validate(response, context)
            except (ValueError, KeyError) as error:
                attempts = repair["repair_count"] if repair else 0
                fields = (
                    [
                        {
                            "field": ".".join(map(str, e["loc"])),
                            "type": e["type"],
                            "message": e["msg"],
                        }
                        for e in error.errors(
                            include_input=False, include_context=False, include_url=False
                        )
                    ]
                    if isinstance(error, ValidationError)
                    else [
                        {"field": "references", "type": "invalid_selection", "message": str(error)}
                    ]
                )
                state["model_call_started"] = False
                repair = {
                    **context.saved(),
                    "stage": stage,
                    "response_type": response_type.__name__,
                    "previous_output": content,
                    "errors": fields,
                    "repair_count": min(attempts + 1, self.owner.settings.structure_repair_limit),
                }
                state["repair_input"] = repair
                await self.save_step(step, state)
                if attempts >= self.owner.settings.structure_repair_limit:
                    failure = ExecutionError(
                        Code.WIKI_MODEL_INVALID_RESPONSE,
                        "PERMANENT",
                        "Wiki structure repair exhausted",
                    )
                    self.stop(failure)
                    raise failure from error
                content = None
                continue
            state.pop("repair_input", None)
            return result

    async def compile_document(self, entry: SnapshotEntry) -> WikiPage:
        key = document_page_id(entry)
        if key in self.pages:
            return self.pages[key]
        target = WikiPage(
            page_id=key,
            kind="document",
            title=cast(str, entry.title),
            statements=[],
            child_page_ids=[],
        )
        batch = await asyncio.to_thread(
            self.owner.reader.read_chunks, entry, self.value.artifacts[entry.generation_id]
        )
        context = build_context(
            self.value,
            target,
            chunks=sorted(batch.chunks, key=lambda c: c.ordinal),
            reports=self.reports,
        )
        state = self.step(key, "SOURCE_ANALYSIS")
        if not state.get("pending_review"):
            statements = await self.call_wiki_model(
                context, "SOURCE_ANALYSIS", key, WikiStatementsResponse, bind_statements, state
            )
            target = target.model_copy(update={"statements": statements})
            target = source_page_with_version_notice(
                target, entry, fallback_ref=next(iter(context.chunks.values()))
            )
            state["pending_review"] = {"candidate": target.model_dump(mode="json")}
            state["model_call_started"] = False
            state["stage"] = "CONTENT_REVIEW"
            await self.save_step(key, state)
        return await self.review_page(context, key, state)

    async def review_page(self, context: WikiContext, step: str, state: dict) -> WikiPage:
        page = WikiPage.model_validate(state["pending_review"]["candidate"])
        review = build_review_context(context, page)

        def bind_addition(response, current):
            return (
                bind_statement(response.open_question_addition, current)
                if response.open_question_addition is not None
                else None
            )

        addition = cast(
            WikiStatement | None,
            await self.call_wiki_model(
                review, "CONTENT_REVIEW", step, WikiReviewResponse, bind_addition, state
            ),
        )
        if addition is not None:
            original = next((s for s in page.statements if s.topic == "open_question"), None)
            question = addition
            if original is not None:
                question = original.model_copy(
                    update={
                        "text": original.text + "\n\n审核补充：" + addition.text,
                        "chunk_refs": list(
                            {
                                (r.generation_id, r.chunk_id): r
                                for r in [*original.chunk_refs, *addition.chunk_refs]
                            }.values()
                        ),
                        "evidence_level": next(
                            level
                            for level in ("inferred", "derived", "direct")
                            if level in (original.evidence_level, addition.evidence_level)
                        ),
                    }
                )
            page = page.model_copy(
                update={
                    "statements": [s for s in page.statements if s.topic != "open_question"]
                    + [question]
                }
            )
        return await self.save_page(step, page)

    async def classify(self, documents: list[WikiPage]) -> list[WikiThemePlan]:
        if "themes" in self.checkpoint:
            return [WikiThemePlan.model_validate(v) for v in self.checkpoint["themes"]]
        step = "theme_classification"
        state = self.step(step, "THEME_CLASSIFICATION")
        themes = cast(
            list[WikiThemePlan],
            await self.call_wiki_model(
                build_classification_context(self.value, documents),
                "THEME_CLASSIFICATION",
                step,
                WikiThemeClassificationResponse,
                bind_themes,
                state,
            ),
        )

        def merge():
            self.checkpoint["themes"] = [t.model_dump(mode="json") for t in themes]
            self.checkpoint.setdefault("steps", {}).pop(step, None)

        await self.save(merge)
        return themes

    async def native_tool_loop(
        self,
        context: WikiContext,
        stage: str,
        step: str,
        state: dict,
        tool: dict,
        load: Callable[[WikiContext, dict], Awaitable[Any]],
        bind: Callable[[WikiContext, Any], dict],
    ) -> tuple[WikiContext, str]:
        if "tool_session" not in state:
            state["tool_session"] = {**context.saved(), "messages": []}
            await self.save_step(step, state)
        session = state["tool_session"]
        context = WikiContext.restored(session)
        while "final_content" not in session:
            completed = {m["tool_call_id"] for m in session["messages"] if m["role"] == "tool"}
            pending = [
                c
                for m in session["messages"]
                if m["role"] == "assistant"
                for c in m.get("tool_calls", [])
                if c["id"] not in completed
            ]
            frozen = WikiContext.restored(json.loads(encode(context.saved())))
            if "request_bindings" in session:
                frozen.chunks = {
                    k: WikiChunkRef.model_validate(v)
                    for k, v in session["request_bindings"]["chunks"].items()
                }
                frozen.pages = dict(session["request_bindings"]["pages"])

            async def read(call, frozen=frozen):
                try:
                    if not await self.eligible():
                        raise asyncio.CancelledError()
                    return await load(frozen, call["function"]), None
                except ValueError as read_error:
                    return None, {"parameter_error": str(read_error)}
                except BaseException as read_error:
                    self.stop(read_error)
                    raise

            tasks = [asyncio.create_task(read(call)) for call in pending]
            try:
                for call, task in zip(pending, tasks, strict=True):
                    raw, error = await asyncio.shield(task)
                    observation = error if error is not None else bind(context, raw)
                    session["messages"].append(
                        {"role": "tool", "tool_call_id": call["id"], "content": encode(observation)}
                    )
                    session.update(context.saved())
                    await self.save_step(step, state)
            except BaseException as error:
                self.stop(error)
                await gather_settled(*tasks)
                raise
            session["request_bindings"] = {
                "chunks": context.saved()["chunks"],
                "pages": dict(context.pages),
            }
            message = await self.request_model(
                context,
                stage,
                step,
                state,
                context.messages(stage, history=session["messages"]),
                tools=[tool],
            )
            session["messages"].append(message)
            if not message.get("tool_calls"):
                session["final_content"] = message["content"]
            state["model_call_started"] = False
            await self.save_step(step, state)
        return context, session["final_content"]

    async def compile_theme(self, theme: WikiThemePlan) -> WikiPage:
        step = theme.page_id
        if step in self.pages:
            return self.pages[step]
        previous = self.base.pages.get(step) if self.base is not None else None
        stage = "THEME_UPDATE" if previous is not None else "THEME_SYNTHESIS"
        target = WikiPage(
            page_id=step,
            kind="synthesis",
            title=theme.title,
            statements=[],
            child_page_ids=theme.source_page_ids,
        )
        members = [self.pages[k] for k in theme.source_page_ids]
        source_ids = (
            self.checkpoint["theme_source_updates"][step]
            if previous is not None
            else theme.source_page_ids
        )
        context = (
            build_theme_update_context(
                self.value, previous, members, [self.pages[k] for k in source_ids]
            )
            if previous is not None
            else build_context(
                self.value,
                target,
                chunks=[],
                reports={},
                input_pages=members,
            )
        )
        state = self.step(step, stage)

        async def load(current: WikiContext, function: dict):
            if function["name"] != "chunk_read":
                raise ValueError("Only chunk_read is available")
            args = ChunkReadArguments.model_validate_json(function["arguments"])
            if not set(args.chunk_refs) <= current.chunks.keys():
                raise ValueError("Select supplied C labels")
            entries = [e for e in self.value.snapshot.entries if document_page_id(e) in source_ids]
            return await run_io(
                lambda: asyncio.to_thread(
                    self.owner.reader.read_source_chunks,
                    [current.chunks[k] for k in dict.fromkeys(args.chunk_refs)],
                    entries,
                    self.value.artifacts,
                    1,
                ),
                self.owner.tool_pool,
                self.eligible,
            )

        def bind(current: WikiContext, chunks) -> dict:
            entries = [e for e in self.value.snapshot.entries if document_page_id(e) in source_ids]
            return self.chunk_observation(current, chunks, entries)

        context, content = await self.native_tool_loop(
            context,
            stage,
            step,
            state,
            {
                "type": "function",
                "function": {
                    "name": "chunk_read",
                    "description": "读取本次来源 C 对应的完整原文、适用上下文及同代次相邻块。",
                    "parameters": ChunkReadArguments.model_json_schema(),
                },
            },
            load,
            bind,
        )
        statements = await self.call_wiki_model(
            context,
            stage,
            step,
            WikiThemeUpdateResponse if previous is not None else WikiStatementsResponse,
            (lambda response, current: bind_theme_update(response, current, previous))
            if previous is not None
            else bind_statements,
            state,
            content=content,
        )
        return await self.save_page(step, target.model_copy(update={"statements": statements}))

    async def incremental_themes(self, documents: list[WikiPage]) -> list[WikiThemePlan]:
        if "themes" in self.checkpoint:
            return [WikiThemePlan.model_validate(v) for v in self.checkpoint["themes"]]
        base = cast(WikiPackage, self.base)
        old_pages = [base.pages[k] for k in base.pages["overview"].child_page_ids]
        previous = [
            WikiThemePlan(page_id=p.page_id, title=p.title, source_page_ids=p.child_page_ids)
            for p in old_pages
        ]
        new_sources = [p for p in documents if p.page_id not in base.pages]
        step, stage = "theme_incremental_classification", "THEME_INCREMENTAL_CLASSIFICATION"
        state = self.step(step, stage)
        if new_sources:
            context = build_incremental_classification_context(self.value, new_sources, old_pages)

            async def load(current: WikiContext, function: dict):
                if function["name"] != "wiki_read":
                    raise ValueError("Only wiki_read is available")
                args = WikiReadArguments.model_validate_json(function["arguments"])
                if args.page_ref not in current.request["existing_scenarios"]:
                    raise ValueError("Select an existing synthesis W")
                return base.pages[current.pages[args.page_ref]]

            def bind(current: WikiContext, page: WikiPage) -> dict:
                for key in page.child_page_ids:
                    if key not in current.pages.values():
                        label = f"W{len(current.pages) + 1}"
                        current.pages[label] = key
                        current.request["directory"][label] = {
                            "title": base.pages[key].title,
                            "kind": "document",
                        }
                return {
                    "status": "completed",
                    "page": page_with_references(page, current.pages),
                    "directory": current.request["directory"],
                }

            context, content = await self.native_tool_loop(
                context,
                stage,
                step,
                state,
                {
                    "type": "function",
                    "function": {
                        "name": "wiki_read",
                        "description": "读取固定基线中的一张已有综合页及其成员目录。",
                        "parameters": WikiReadArguments.model_json_schema(),
                    },
                },
                load,
                bind,
            )
            themes, updates = cast(
                tuple[list[WikiThemePlan], dict[str, list[str]]],
                await self.call_wiki_model(
                    context,
                    stage,
                    step,
                    WikiIncrementalClassificationResponse,
                    lambda response, current: bind_incremental_themes(response, current, previous),
                    state,
                    content=content,
                ),
            )
        else:
            themes, updates = previous, {}
        for theme in previous:
            changed = [k for k in theme.source_page_ids if k in self.changed_source_ids]
            if changed:
                updates[theme.page_id] = list(
                    dict.fromkeys([*updates.get(theme.page_id, []), *changed])
                )

        def merge():
            self.checkpoint["themes"] = [t.model_dump(mode="json") for t in themes]
            self.checkpoint["theme_source_updates"] = updates
            self.checkpoint.setdefault("steps", {}).pop(step, None)
            for page in old_pages:
                if page.page_id not in updates:
                    self.pages[page.page_id] = page
                    self.checkpoint.setdefault("pages", {})[page.page_id] = page.model_dump(
                        mode="json"
                    )

        await self.save(merge)
        return themes

    def chunk_observation(self, context: WikiContext, chunks, entries: list[SnapshotEntry]) -> dict:
        reverse = {(r.generation_id, r.chunk_id): label for label, r in context.chunks.items()}
        sources = {e.generation_id: f"D{i}" for i, e in enumerate(entries, 1)}
        result = {}
        for chunk in chunks:
            key = chunk.generation_id, chunk.chunk_id
            if key not in reverse:
                label = f"C{len(context.chunks) + 1}"
                context.chunks[label] = WikiChunkRef(
                    generation_id=chunk.generation_id, chunk_id=chunk.chunk_id
                )
                context.request["chunks"][label] = {"source_ref": sources[chunk.generation_id]}
                reverse[key] = label
            result[reverse[key]] = {
                "source_ref": sources[chunk.generation_id],
                **chunk_content(chunk, [e.title for e in entries if e.title]),
            }
        return {"status": "completed", "chunks": result}

    async def run(self) -> WorkflowSucceeded:
        try:
            return await self.compile()
        except BaseException as error:
            self.stop(error)
            if self.budget_exhausted:

                def finish_budget():
                    with self.owner.repository.session() as session:
                        self.owner.state.check_execution(session, self.ctx)
                        session.commit()

                try:
                    await asyncio.to_thread(finish_budget)
                except Exception as budget_error:
                    self.stop(budget_error)
            if self.failure is not error:
                raise cast(BaseException, self.failure) from error
            raise

    async def compile(self) -> WorkflowSucceeded:
        with self.owner.repository.session() as session:
            build = self.owner.repository.load_build(session, self.ctx.job_id)
            self.name, _ = self.owner.repository.lock_knowledge_base(session, build["kb_id"])
        self.value = build["input"]
        self.checkpoint = dict(build["checkpoint"])
        if any(s.get("model_call_started") for s in self.checkpoint.get("steps", {}).values()):
            raise ExecutionError(
                Code.WIKI_MODEL_OUTCOME_UNKNOWN,
                "OUTCOME_UNKNOWN",
                "Previous model call has no reliable committed output",
            )
        if not await self.eligible():
            raise asyncio.CancelledError()
        if self.checkpoint.get("result_ref"):
            return await self.publish()
        self.base = (
            await asyncio.to_thread(
                self.owner.reader.read_package, self.value.base_ref, self.value.snapshot.kb_id
            )
            if self.value.base_ref is not None
            else None
        )
        if self.value.edited_pages is not None:
            self.pages = self.value.edited_pages
            self.reports = cast(WikiPackage, self.base).parse_reports
        else:
            await self.compile_automatic()
        if self.stopping:
            raise asyncio.CancelledError()
        package = WikiPackage(
            job_id=self.ctx.job_id,
            snapshot=self.value.snapshot,
            artifacts=self.value.artifacts,
            parse_reports=self.reports,
            pages=self.pages,
        )
        try:
            ref = await asyncio.to_thread(
                self.owner.store.write_artifact,
                package.model_dump_json().encode(),
                media_type="application/json",
                artifact_kind="WIKI",
            )
        except (OSError, ValueError) as error:
            raise ExecutionError(
                Code.WIKI_ARTIFACT_ERROR, "OUTCOME_UNKNOWN", "Wiki artifact write outcome unknown"
            ) from error
        self.checkpoint["result_ref"] = ref.model_dump(mode="json")
        await self.save()
        return await self.publish()

    async def reusable_pages(self, base: WikiPackage | None) -> dict[str, WikiPage]:
        candidates = select_reusable_pages(self.value, base)
        if base is None:
            return {}
        reusable = {}
        previous_entries = {document_page_id(e): e for e in base.snapshot.entries}
        for entry in self.value.snapshot.entries:
            key = document_page_id(entry)
            if key not in candidates:
                continue
            decision = self.checkpoint.get("source_reuse", {}).get(key)
            if decision is not None:
                if decision:
                    reusable[key] = self.pages[key]
                continue
            page = candidates[key]
            previous = previous_entries[key]
            fallback_ref = None
            step = f"source_references:{key}"
            if previous.generation_id != entry.generation_id:
                old, current = await asyncio.gather(
                    asyncio.to_thread(
                        self.owner.reader.read_chunks,
                        previous,
                        base.artifacts[previous.generation_id],
                    ),
                    asyncio.to_thread(
                        self.owner.reader.read_chunks,
                        entry,
                        self.value.artifacts[entry.generation_id],
                    ),
                )
                page, unresolved = rebind_exact_references(page, old.chunks, current.chunks)
                if unresolved:
                    stage = "SOURCE_REFERENCE_REBINDING"
                    references = cast(
                        dict[str, list[WikiChunkRef]],
                        await self.call_wiki_model(
                            build_reference_rebinding_context(
                                entry, previous, page, old.chunks, current.chunks, unresolved
                            ),
                            stage,
                            step,
                            WikiReferenceRebindingResponse,
                            bind_reference_rebinding,
                            self.step(step, stage),
                        ),
                    )
                    if not all(references.values()):

                        def rejected(key=key, step=step):
                            self.checkpoint.setdefault("source_reuse", {})[key] = False
                            self.checkpoint.setdefault("steps", {}).pop(step, None)

                        await self.save(rejected)
                        continue
                    page = page.model_copy(
                        update={
                            "statements": [
                                s.model_copy(update={"chunk_refs": references[f"S{i}"]})
                                if f"S{i}" in references
                                else s
                                for i, s in enumerate(page.statements, 1)
                            ]
                        }
                    )
                chunk = current.chunks[0]
                fallback_ref = WikiChunkRef(
                    generation_id=chunk.generation_id, chunk_id=chunk.chunk_id
                )
            if (
                source_version_notice(entry)
                and fallback_ref is None
                and not any(s.chunk_refs for s in page.statements)
            ):
                batch = await asyncio.to_thread(
                    self.owner.reader.read_chunks, entry, self.value.artifacts[entry.generation_id]
                )
                chunk = batch.chunks[0]
                fallback_ref = WikiChunkRef(
                    generation_id=chunk.generation_id, chunk_id=chunk.chunk_id
                )
            page = source_page_with_version_notice(
                page, entry, previous=previous, fallback_ref=fallback_ref
            )

            def accepted(key=key, page=page, step=step):
                self.pages[key] = page
                self.checkpoint.setdefault("pages", {})[key] = page.model_dump(mode="json")
                self.checkpoint.setdefault("source_reuse", {})[key] = True
                self.checkpoint.setdefault("steps", {}).pop(step, None)

            await self.save(accepted)
            reusable[key] = page
        return reusable

    async def compile_automatic(self) -> None:
        base = self.base
        self.pages = {
            k: WikiPage.model_validate(v) for k, v in self.checkpoint.get("pages", {}).items()
        }
        reusable = {} if self.value.rebuild_sources else await self.reusable_pages(base)
        self.changed_source_ids = {
            document_page_id(e) for e in self.value.snapshot.entries
        } - reusable.keys()
        # Completed unpublished packages may fill exact source-cache misses only.
        cache = None
        if not self.value.rebuild_sources:
            with self.owner.repository.session() as session:
                latest = self.owner.repository.load_latest_build(
                    session, self.value.snapshot.kb_id, successful=True
                )
            if latest is not None:
                cache = await asyncio.to_thread(
                    self.owner.reader.read_package,
                    ArtifactRef.model_validate(latest["checkpoint"]["result_ref"]),
                    self.value.snapshot.kb_id,
                )
                for key, page in (await self.reusable_pages(cache)).items():
                    reusable.setdefault(key, page)
        for key, page in reusable.items():
            self.pages.setdefault(key, page)
        self.checkpoint["pages"] = {k: p.model_dump(mode="json") for k, p in self.pages.items()}
        await self.save()
        for entry in self.value.snapshot.entries:
            artifacts = self.value.artifacts[entry.generation_id]
            matched = next(
                (
                    p
                    for p in (base, cache)
                    if p is not None and p.artifacts.get(entry.generation_id) == artifacts
                ),
                None,
            )
            if matched is not None:
                self.reports[entry.generation_id] = matched.parse_reports[entry.generation_id]
            else:
                parsed = await asyncio.to_thread(self.owner.reader.read_parsed, entry, artifacts)
                self.reports[entry.generation_id] = parsed.parse_report
        if (
            base is not None
            and not self.changed_source_ids
            and {document_page_id(e) for e in self.value.snapshot.entries}
            == {key for key, page in base.pages.items() if page.kind == "document"}
        ):
            self.pages = {**base.pages, **self.pages}
            return
        entries = sorted(
            self.value.snapshot.entries,
            key=lambda e: (
                e.source.document_id,
                e.policy_metadata.effective_from.value if e.policy_metadata.effective_from else "",
                e.source.source_hash,
            ),
        )
        documents = await self.parallel([self.compile_document(e) for e in entries])
        themes = (
            await self.incremental_themes(documents)
            if base is not None
            else await self.classify(documents)
        )
        synthesized = await self.parallel([self.compile_theme(theme) for theme in themes])
        if "overview" in self.pages:
            return
        root = WikiPage(
            page_id="overview",
            kind="overview",
            title=f"{self.name}制度全景",
            statements=[],
            child_page_ids=[p.page_id for p in synthesized],
        )
        if base is not None:
            previous = base.pages["overview"]
            changed = {p.page_id for p in synthesized if p != base.pages.get(p.page_id)}
            if not changed:
                await self.save_page("overview", previous)
                return
            context = build_overview_update_context(
                self.name, previous, synthesized, set(previous.child_page_ids), changed
            )
            stage, response_type = "OVERVIEW_UPDATE", WikiOverviewUpdateResponse

            def validate(response, context):
                return bind_overview_update(response, context, previous)
        else:
            context = build_overview_context(self.name, synthesized)
            stage, response_type, validate = "OVERVIEW", WikiOverviewResponse, bind_overview
        statements = await self.call_wiki_model(
            context, stage, "overview", response_type, validate, self.step("overview", stage)
        )
        await self.save_page("overview", root.model_copy(update={"statements": statements}))

    async def publish(self) -> WorkflowSucceeded:
        def write():
            with self.owner.repository.session() as session:
                if not self.owner.state.check_execution(session, self.ctx):
                    session.commit()
                    return False
                _, revision = self.owner.repository.lock_knowledge_base(
                    session, self.value.snapshot.kb_id
                )
                if revision == self.value.snapshot.revision:
                    self.owner.repository.set_published_build(
                        session, self.value.snapshot.kb_id, self.ctx.job_id
                    )
                elif self.value.edited_pages is not None:
                    raise ExecutionError(
                        Code.WIKI_SOURCE_REVISION_CONFLICT,
                        "PERMANENT",
                        "Sources changed; edited version was not published",
                    )
                self.owner.state.complete(session, self.ctx)
                session.commit()
                return True

        try:
            if not await asyncio.to_thread(write):
                raise asyncio.CancelledError()
        except SQLAlchemyError as error:
            logger.error(
                "Wiki publication unresolved job_id=%s kb_id=%s %s",
                self.ctx.job_id,
                self.value.snapshot.kb_id,
                exception_diagnostic(error),
            )
            raise ExecutionError(
                Code.WIKI_ARTIFACT_ERROR, "OUTCOME_UNKNOWN", "Wiki publication commit unresolved"
            ) from error
        return WorkflowSucceeded()
