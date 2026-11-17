"""Per-item retrieval scope, native options and completed business actions."""

from collections.abc import Mapping
from typing import Literal, NotRequired, TypedDict
from unicodedata import normalize
from uuid import UUID, uuid4

from pydantic import JsonValue

from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    Chunk,
    KnowledgeSnapshot,
    SnapshotEntry,
)
from intelligent_reimbursement_assitant.document_ingestion_service.domain.policy_metadata import (
    model_policy_date,
    overlaps_policy,
)
from intelligent_reimbursement_assitant.model_gateway.domain.model_text import (
    prepare_model_text,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.model_inputs import (
    bind,
    fact_context,
    period_context,
    share_context_texts,
    task_context,
)
from intelligent_reimbursement_assitant.policy_qa_service.domain.models import (
    FactValue,
    ItemAssessment,
    SearchBatch,
    SearchQuery,
    TaskItem,
    TaskPlan,
)


def select_snapshot_entries(item: TaskItem, snapshot: KnowledgeSnapshot) -> list[SnapshotEntry]:
    if item.date_from is None or item.date_to is None:
        return list(snapshot.entries)
    return [
        entry
        for entry in snapshot.entries
        if overlaps_policy(entry.policy_metadata, item.date_from, item.date_to)
    ]


def snapshot_groups(entries: list[SnapshotEntry]) -> dict[str, set[UUID]]:
    groups: dict[str, set[UUID]] = {}
    for entry in entries:
        groups.setdefault(entry.index_profile_id, set()).add(entry.generation_id)
    return groups


def select_retrieval_items(plan: TaskPlan, assessments: list[ItemAssessment]) -> list[TaskItem]:
    if not assessments:
        return list(plan.items)
    affected = {a.item_id for a in assessments if any(g.kind == "POLICY_MISSING" for g in a.gaps)}
    return [item for item in plan.items if item.item_id in affected]


def query_entries(query: SearchQuery, entries: list[SnapshotEntry]) -> list[SnapshotEntry]:
    return [entry for entry in entries if entry.source.document_id in query.document_ids]


def matching_search_batches(
    batches: list[SearchBatch], query: SearchQuery, entries: list[SnapshotEntry]
) -> list[SearchBatch]:
    groups = snapshot_groups(query_entries(query, entries))
    return [
        batch
        for batch in batches
        if batch.query_id == query.query_id
        and batch.mode == query.mode
        and batch.index_profile_id in groups
        and set(batch.generation_ids) == groups[batch.index_profile_id]
    ]


def search_completed(
    batches: list[SearchBatch], query: SearchQuery, entries: list[SnapshotEntry]
) -> bool:
    matched = matching_search_batches(batches, query, entries)
    groups = snapshot_groups(query_entries(query, entries))
    return bool(groups) and {batch.index_profile_id for batch in matched} == groups.keys()


def accept_search_query(
    queries: list[SearchQuery],
    item_id: str,
    mode: Literal["keyword", "semantic"],
    text: str,
    document_ids: list[UUID],
    retrieval_round: int,
) -> tuple[list[SearchQuery], SearchQuery]:
    old = next((q for q in queries if q.item_id == item_id and q.mode == mode), None)

    def key(value: str) -> str:
        return normalize("NFKC", " ".join(value.split())).casefold()

    same = (
        old is not None
        and key(old.text) == key(text)
        and set(old.document_ids) == set(document_ids)
    )
    query = SearchQuery(
        query_id=old.query_id if same and old is not None else str(uuid4()),
        item_id=item_id,
        mode=mode,
        text=old.text if same and old is not None else " ".join(text.split()),
        document_ids=sorted(document_ids),
        retrieval_round=retrieval_round,
    )
    return [q for q in queries if not (q.item_id == item_id and q.mode == mode)] + [query], query


def item_candidates(
    item_id: str,
    queries: list[SearchQuery],
    batches: list[SearchBatch],
    ranked_ids: dict[str, list[str]],
    retrieval_round: int,
) -> list[Chunk]:
    query_ids = {
        q.query_id for q in queries if q.item_id == item_id and q.retrieval_round == retrieval_round
    }
    chunks = {
        chunk.chunk_id: chunk
        for batch in batches
        if batch.query_id in query_ids
        for chunk in batch.chunks
    }
    return [chunks[key] for key in ranked_ids.get(item_id, []) if key in chunks]


class ReadWindow(TypedDict):
    seed_ids: list[str]
    generation_ids: list[str]
    neighbor_radius: int
    returned_ids: list[str]


class ChunkHistory(TypedDict):
    shown_rounds: list[int]
    read: bool


class ToolSession(TypedDict):
    wiki_bindings: dict[str, str]
    wiki_read_page_ids: list[str]
    wiki_free_source_page_ids: list[str]
    policy_bindings: dict[str, str]
    retrieval_round: int
    handoff_round: int | None
    chunk_bindings: dict[str, str]
    read_windows: list[ReadWindow]
    messages: list[dict[str, JsonValue]]


class ToolExecutionResult(TypedDict):
    status: Literal["completed", "not_executed"]
    hit_count: NotRequired[int]
    returned_chunk_ids: NotRequired[list[str]]
    reason: NotRequired[str]
    retrieval_closed: NotRequired[bool]
    remaining_chunk_slots: NotRequired[int]
    additional_chunk_count: NotRequired[int]
    tool_response_error: NotRequired[str]
    wiki_page: NotRequired[dict]


def new_tool_session(retrieval_round: int) -> ToolSession:
    return {
        "wiki_bindings": {},
        "wiki_read_page_ids": [],
        "wiki_free_source_page_ids": [],
        "policy_bindings": {},
        "retrieval_round": retrieval_round,
        "handoff_round": None,
        "chunk_bindings": {},
        "read_windows": [],
        "messages": [],
    }


def chunk_heading(chunk: Chunk) -> dict:
    headings = list(
        dict.fromkeys(
            " / ".join(locator.section_path) for locator in chunk.locators if locator.section_path
        )
    )
    return {"heading": "；".join(headings)} if headings else {}


def build_policy_catalog(
    entries: list[SnapshotEntry],
    policy_bindings: Mapping[str, str],
) -> tuple[dict[str, dict], dict[UUID, str], dict[str, str]]:
    bindings = dict(policy_bindings)
    documents = {UUID(key): ref for ref, key in bindings.items()}
    policies: dict[str, dict] = {}
    revisions = {}
    generation_refs = {}
    for entry in entries:
        document = entry.source.document_id
        if document not in documents:
            label = f"P{len(bindings) + 1}"
            documents[document] = label
            bindings[label] = str(document)
        label = documents[document]
        metadata = entry.policy_metadata
        policies.setdefault(
            label, {"title": entry.title, "issuer": metadata.issuer, "revisions": {}}
        )
        identity = entry.source
        if identity not in revisions:
            revisions[identity] = f"R{len(revisions) + 1}"
            policies[label]["revisions"][revisions[identity]] = {
                "version_status": metadata.version_status.value,
                "effective_from": model_policy_date(metadata.effective_from),
                "effective_to": model_policy_date(metadata.effective_to),
                "policy_code": metadata.policy_code,
            }
        generation_refs[entry.generation_id] = revisions[identity]
    return policies, generation_refs, bindings


def build_tool_context(
    item: TaskItem,
    plan: TaskPlan,
    queries: list[SearchQuery],
    search_batches: list[SearchBatch],
    ranked_ids: dict[str, list[str]],
    evidence_chunks: dict[str, Chunk],
    evidence_ids: list[str],
    entries: list[SnapshotEntry],
    session: ToolSession,
    assessments: list[ItemAssessment],
    *,
    chunk_history: Mapping[str, ChunkHistory],
    retrieval_round: int,
    remaining_tool_steps: int,
    remaining_chunk_slots: int,
    neighbor_radius: int,
    retrieval_open: bool = True,
    preview_limit: int,
    fact_values: Mapping[str, FactValue],
    wiki_context: dict | None = None,
) -> tuple[dict[str, object], list[dict[str, JsonValue]], ToolSession]:
    item_queries = [q for q in queries if q.item_id == item.item_id]
    current = [q for q in item_queries if q.retrieval_round == retrieval_round]
    valid_batches = [
        batch
        for query in current
        for batch in matching_search_batches(search_batches, query, entries)
    ]
    candidates = item_candidates(item.item_id, queries, valid_batches, ranked_ids, retrieval_round)
    generations = {entry.generation_id for entry in entries}
    full_ids = list(
        dict.fromkeys(
            [
                *evidence_ids,
                *(key for window in session["read_windows"] for key in window["returned_ids"]),
            ]
        )
    )
    full_ids = [key for key in full_ids if evidence_chunks[key].generation_id in generations]
    chunks = {chunk.chunk_id: chunk for chunk in candidates[:preview_limit]}
    chunks.update({key: evidence_chunks[key] for key in full_ids})
    for key in chunks:
        if key in chunk_history and chunk_history[key]["read"]:
            chunks[key] = evidence_chunks[key]
            if key not in full_ids:
                full_ids.append(key)
    bindings = dict(session["chunk_bindings"])
    reverse = {key: ref for ref, key in bindings.items()}
    for key in chunks:
        if key not in reverse:
            ref = f"C{len(bindings) + 1}"
            bindings[ref] = key
            reverse[key] = ref
    policies, generation_refs, policy_bindings = build_policy_catalog(
        entries, session["policy_bindings"]
    )
    documents = {UUID(key): ref for ref, key in policy_bindings.items()}
    searches = {}
    for query in item_queries:
        matched = matching_search_batches(search_batches, query, entries)
        completed = search_completed(search_batches, query, entries)
        searches[query.mode] = {
            "text": query.text,
            "policy_refs": [documents[key] for key in query.document_ids if key in documents],
            "current_round": query.retrieval_round == retrieval_round,
            "status": "completed" if completed else "not_executed",
            **(
                {"hit_count": len({c.chunk_id for b in matched for c in b.chunks})}
                if completed
                else {}
            ),
        }
    available_modes = [
        mode
        for mode in ("keyword", "semantic")
        if retrieval_open
        and remaining_tool_steps > 0
        and entries
        and mode not in {q.mode for q in current}
    ]
    prepared_texts = {
        key: prepare_model_text(
            chunk.body_text, [entry.title for entry in entries if entry.title is not None]
        )
        for key, chunk in chunks.items()
    }
    context = share_context_texts(
        {
            "task": task_context(item),
            "period": period_context(item),
            "facts": {
                ref: fact_context(fact, fact_values)
                for ref, fact in bind(
                    "F", [f for f in plan.facts if f.fact_id in item.fact_ids]
                ).items()
            },
            "searches": searches,
            "policy_gaps": [
                gap.description
                for a in assessments
                if a.item_id == item.item_id
                for gap in a.gaps
                if gap.kind == "POLICY_MISSING"
            ],
            "chunks": {
                reverse[key]: {
                    "revision_ref": generation_refs[chunk.generation_id],
                    **chunk_heading(chunk),
                    "content": prepared_texts[key]
                    if key in full_ids
                    else prepared_texts[key][:500],
                    "content_kind": "full" if key in full_ids else "preview",
                    "seen_in_previous_round": retrieval_round - 1
                    in chunk_history.get(key, {"shown_rounds": [], "read": False})["shown_rounds"],
                    "read_before": chunk_history.get(key, {"shown_rounds": [], "read": False})[
                        "read"
                    ],
                    "adopted": key in evidence_ids,
                }
                for key, chunk in chunks.items()
            },
            "limits": {
                "remaining_tool_steps": remaining_tool_steps,
                "available_search_modes": available_modes,
                "remaining_chunk_slots": remaining_chunk_slots,
                **(
                    {"neighbor_radius": neighbor_radius}
                    if retrieval_open and entries and chunks
                    else {}
                ),
            },
        }
    )
    context["policies"] = policies
    if wiki_context is not None:
        read_pages = set(session["wiki_read_page_ids"])
        read_refs = [
            ref for ref, page_id in session["wiki_bindings"].items() if page_id in read_pages
        ]
        context["wiki"] = {
            **{
                k: v
                for k, v in wiki_context.items()
                if k != "chunks"
                and not (wiki_context.get("page_ref") in read_refs and k in ("sections", "sources"))
            },
            "read_page_refs": read_refs,
        }
        context["chunks"] = {
            **{
                ref: {
                    **navigation,
                    "seen_in_previous_round": retrieval_round - 1
                    in chunk_history.get(bindings[ref], {"shown_rounds": [], "read": False})[
                        "shown_rounds"
                    ],
                    "read_before": chunk_history.get(
                        bindings[ref], {"shown_rounds": [], "read": False}
                    )["read"],
                    "adopted": bindings[ref] in evidence_ids,
                }
                for ref, navigation in wiki_context.get("chunks", {}).items()
            },
            **context["chunks"],
        }
    tools: list[dict[str, JsonValue]] = []
    if (
        retrieval_open
        and (remaining_tool_steps > 0 or session["wiki_free_source_page_ids"])
        and session["wiki_bindings"]
        and wiki_context is not None
    ):
        tools.append(
            _tool(
                "wiki_read",
                TOOL_DESCRIPTIONS["wiki_read"],
                {"page_ref": {"type": "string", "minLength": 1}},
            )
        )
    if retrieval_open and entries:
        for mode in available_modes:
            name = f"{mode}_search"
            tools.append(
                _tool(
                    name,
                    TOOL_DESCRIPTIONS[name],
                    {
                        "query": {"type": "string", "minLength": 1},
                        "policy_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "uniqueItems": True,
                        },
                    },
                )
            )
    if (
        retrieval_open
        and remaining_tool_steps > 0
        and entries
        and (chunks or (wiki_context and wiki_context.get("chunks")))
    ):
        tools.append(
            _tool(
                "chunk_read",
                TOOL_DESCRIPTIONS["chunk_read"],
                {
                    "chunk_refs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                        "uniqueItems": True,
                    }
                },
            )
        )
    tools.append(build_finish_schema())
    return (
        context,
        tools,
        {**session, "chunk_bindings": bindings, "policy_bindings": policy_bindings},
    )


def build_finish_schema() -> dict[str, JsonValue]:
    return _tool(
        "finish_retrieval",
        TOOL_DESCRIPTIONS["finish_retrieval"],
        {
            "chunk_refs": {"type": "array", "items": {"type": "string"}, "uniqueItems": True},
            "explanation": {"type": "string", "minLength": 1},
        },
    )


def select_handoff_chunks(
    selected_refs: list[str],
    session: ToolSession,
    batches: list[SearchBatch],
    evidence_chunks: dict[str, Chunk],
    evidence_ids: list[str],
) -> list[Chunk]:
    available = {chunk.chunk_id: chunk for batch in batches for chunk in batch.chunks}
    available.update(
        {
            key: evidence_chunks[key]
            for key in [
                *evidence_ids,
                *(key for window in session["read_windows"] for key in window["returned_ids"]),
            ]
        }
    )
    if any(session["chunk_bindings"][ref] not in available for ref in selected_refs):
        raise ValueError("Wiki references require original Chunk content before handoff")
    return [available[session["chunk_bindings"][ref]] for ref in selected_refs]


def _tool(name: str, description: str, properties: dict) -> dict[str, JsonValue]:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(properties),
                "additionalProperties": False,
            },
        },
    }


TOOL_DESCRIPTIONS = {
    "wiki_read": """Purpose: 渐进读取当前知识库的制度概览、业务关联和分块导航。
Use When: 需要了解制度分工、场景、流程或继续展开W目录。
当你发现了用户问题或事实中某些需要证实的隐含前提并了解它们的业务含义，但很可能无法通过其他查询方式获取。例如用户想要用某类经费报销某活动，但并没有明确该经费包含这种用途、且在当前搜索中没有获得相关信息，可以使用这种查询方式。
Do Not Use When: 需要以摘要代替正式制度证据；页面位于wiki.read_page_refs且历史观察中的内容已足够。
Preconditions: page_ref选择当前提供的一个W标签；程序固定知识库、版本和日期范围。
Output Semantics: 概览提供综合主题W；综合页提供制度关系及成员来源页W；来源页提供单份制度导览及C导航。已读W进入wiki.read_page_refs，正文保留于历史工具观察。综合页每次读取计一步；同轮已读任一综合页引用的未读来源页首次读取不计次，其他页面读取及任何重复读取均计一步。新轮重新累计免计次范围，chunk_read正常计次。C的wiki_reference仅为导航，须显式chunk_read或搜索取得真实原文后交接。页面不会自动读取原文。""",
    "keyword_search": """Purpose: 按精确词语定位制度条款或表格，可限定制度。
Use When: 已知制度、经费类别、等级、文号或目标行；例如 query=五类 住宿标准 科研经费，policy_refs=[P3]。
Do Not Use When: 只有宽泛目标且缺少术语；本轮已用关键词查询；已有证据充分。
Preconditions: query为非空关键词字符串，policy_refs选择当前P目录或空列表；程序控制预算和本轮位置。
Output Semantics: completed返回真实去重hit_count；0为真实零命中；not_executed不代表制度没有规定，候选预览进入C目录。""",
    "semantic_search": """Purpose: 按完整问题语义检索制度原文，可限定制度。
Use When: 需要目标、决定性事实或同义表达；例如 query=科研经费出差五类人员在上海的每晚住宿上限是多少，policy_refs=[P3]。
Do Not Use When: 只需精确定位已知术语；本轮已用向量查询；输入仅是无关系关键词。
Preconditions: query为包含目标和必要事实的完整句，policy_refs合法；程序生成Embedding并过滤允许代次。
Output Semantics: 返回真实执行状态、去重hit_count及C候选；语义相似仅代表相关性，不证明适用性。""",
    "chunk_read": """Purpose: 读取C目录所选块的完整正文及同代次邻块。
Use When: 预览截断表头、金额行、单位、脚注、例外或必要上下文。
Do Not Use When: 全文已充分；预览足以采用；想跳转到未知块。
Preconditions: chunk_refs为当前C标签非空唯一列表；程序限定邻块半径和全Job块容量。
Output Semantics: 成功原文进入C目录和缓存并标为本项已读；容量不足返回not_executed。""",
    "finish_retrieval": """Purpose: 结束本项本轮工具动作，把采用块和说明交给证据判断阶段，核验制度原文对本项的支持情况。
Use When: 所选预览或全文足以回答本项并补足当前policy_gaps；本轮合理查找仍无法补齐；额度限制阻止继续。
Do Not Use When: 当前缺口仍有合理可用的搜索或读取路径；同批还要执行其他工具；用自由文字替代来源或把初步理解当最终答案。
Preconditions: 必须单独调用；chunk_refs为当前C标签唯一列表，可为空；explanation为不含临时T占位符的非空文字。
Output Semantics: 程序结束本轮并交接完整Chunk、保存采用关系和说明；completed只表示本轮交接成功，不代表其他轮次完成、显式已读或证据判断阶段确认支持。""",
}
