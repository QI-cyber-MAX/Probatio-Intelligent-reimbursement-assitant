"""Stage inputs and identity/reference binding for the three-level Wiki."""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import cast
from uuid import UUID, uuid4

from pydantic import JsonValue

from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    Chunk,
    SnapshotEntry,
    VersionStatus,
)
from intelligent_reimbursement_assitant.document_ingestion_service.domain.policy_metadata import (
    model_policy_date,
)
from intelligent_reimbursement_assitant.knowledge_wiki.domain import prompts
from intelligent_reimbursement_assitant.knowledge_wiki.domain.model_responses import (
    WikiIncrementalClassificationResponse,
    WikiOverviewResponse,
    WikiOverviewUpdateResponse,
    WikiReferenceRebindingResponse,
    WikiReviewResponse,
    WikiStatementResponse,
    WikiStatementsResponse,
    WikiThemeClassificationResponse,
    WikiThemeUpdateResponse,
)
from intelligent_reimbursement_assitant.knowledge_wiki.domain.models import (
    TOPICS,
    WikiBuildInput,
    WikiChunkRef,
    WikiPackage,
    WikiPage,
    WikiStatement,
    WikiThemePlan,
)
from intelligent_reimbursement_assitant.model_gateway.domain.model_text import (
    prepare_model_text,
    restore_texts,
    share_texts,
)
from intelligent_reimbursement_assitant.parser.domain.models import ParseReport


def encode(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def document_page_id(entry: SnapshotEntry) -> str:
    return f"document-{entry.source.document_id}-{entry.source.source_hash}"


def select_reusable_pages(value: WikiBuildInput, base: WikiPackage | None) -> dict[str, WikiPage]:
    if base is None:
        return {}
    old = {(e.source.document_id, e.source.source_hash): e for e in base.snapshot.entries}
    pages = {}
    for entry in value.snapshot.entries:
        previous = old.get((entry.source.document_id, entry.source.source_hash))
        if (
            previous is None
            or previous.title != entry.title
            or previous.policy_metadata.issuer != entry.policy_metadata.issuer
            or previous.policy_metadata.policy_code != entry.policy_metadata.policy_code
        ):
            continue
        key = document_page_id(entry)
        pages[key] = base.pages[key]
    return pages


def source_version_notice(entry: SnapshotEntry) -> str:
    metadata = entry.policy_metadata
    if metadata.version_status != VersionStatus.HISTORICAL:
        return ""
    notice = "该原件已成为历史版本。"
    start, end = metadata.effective_from, metadata.effective_to
    if start is not None and end is not None:
        notice += f"生效日至截止日：{start.value} 至 {end.value}（含生效日、不含截止日）。"
    elif start is not None:
        notice += f"生效日：{start.value}。"
    elif end is not None:
        notice += f"截止日：{end.value}（不含截止日）。"
    return notice


def source_page_with_version_notice(
    page: WikiPage,
    entry: SnapshotEntry,
    *,
    previous: SnapshotEntry | None = None,
    fallback_ref: WikiChunkRef | None = None,
) -> WikiPage:
    notice = source_version_notice(entry)
    old_notice = source_version_notice(previous or entry)
    statements = list(page.statements)
    if statements and statements[0].topic == "business_context":
        first = statements[0]
        text = first.text
        if old_notice and text.startswith(old_notice):
            text = text[len(old_notice) :].removeprefix("\n\n")
        text = "\n\n".join(part for part in (notice, text) if part)
        if text:
            statements[0] = first.model_copy(update={"text": text})
        else:
            statements.pop(0)
    elif notice:
        refs = [r for s in statements for r in s.chunk_refs]
        statements.insert(
            0,
            WikiStatement(
                kind="summary",
                topic="business_context",
                evidence_level="derived",
                text=notice,
                chunk_refs=refs[:1] or ([fallback_ref] if fallback_ref is not None else []),
                related_page_ids=[],
            ),
        )
    return page.model_copy(update={"statements": statements})


@dataclass
class WikiContext:
    request: dict
    chunks: dict[str, WikiChunkRef]
    pages: dict[str, str]

    def model_input(self) -> dict:
        data = dict(self.request)
        # Directory metadata is authoritative; the other collections carry membership/body only.
        for field in ("pages", "new_sources", "delta_sources", "updated_pages", "new_pages"):
            if field in data:
                data[field] = {
                    label: {k: v for k, v in page.items() if k not in {"title", "kind"}}
                    for label, page in data[field].items()
                }
        if "existing_scenarios" in data:
            data["existing_scenarios"] = list(data["existing_scenarios"])
        if "chunks" not in data:
            return data
        if len(data["sources"]) == 1:
            data["chunks"] = {
                label: {k: v for k, v in chunk.items() if k != "source_ref"}
                for label, chunk in data["chunks"].items()
            }
        texts = {"chunks": {k: v["text"] for k, v in data["chunks"].items() if "text" in v}}
        if "current_chunk_refs" in data:
            texts["statements"] = {k: v["text"] for k, v in data["statements"].items()}
        content = share_texts(texts)
        return {
            **data,
            "chunks": {
                k: {**v, **({"text": content["chunks"][k]} if "text" in v else {})}
                for k, v in data["chunks"].items()
            },
            **(
                {
                    "statements": {
                        k: {**v, "text": content["statements"][k]}
                        for k, v in data["statements"].items()
                    }
                }
                if "statements" in content
                else {}
            ),
            **({"texts": content["texts"]} if "texts" in content else {}),
        }

    def messages(
        self, stage: str, repair: dict | None = None, history: list[dict] | None = None
    ) -> list[dict[str, JsonValue]]:
        data = self.model_input()
        if repair and not history:
            data["repair"] = repair
        projected_history = []
        calls = {}
        read_pages = set()
        read_chunks = {k for k, v in data.get("chunks", {}).items() if "text" in v}
        for message in history or []:
            if message["role"] == "assistant":
                calls.update({call["id"]: call for call in message.get("tool_calls", [])})
            if message["role"] == "tool":
                observation = json.loads(message["content"])
                if "page" in observation:
                    page_ref = json.loads(calls[message["tool_call_id"]]["function"]["arguments"])[
                        "page_ref"
                    ]
                    if page_ref in read_pages:
                        observation = {"status": "already_read", "page_ref": page_ref}
                    else:
                        observation = {
                            "status": observation["status"],
                            "page_ref": page_ref,
                            "page": {
                                k: v
                                for k, v in observation["page"].items()
                                if k not in {"title", "kind"}
                            },
                        }
                        read_pages.add(page_ref)
                if "chunks" in observation:
                    chunks = {}
                    for label, chunk in observation["chunks"].items():
                        chunks[label] = (
                            {"read_before": True}
                            if label in read_chunks
                            else {
                                k: v
                                for k, v in chunk.items()
                                if k != "source_ref" or len(data["sources"]) != 1
                            }
                        )
                        read_chunks.add(label)
                    observation = {**observation, "chunks": chunks}
                message = {**message, "content": encode(observation)}
            projected_history.append(message)
        messages = [
            {"role": "system", "content": prompts.WIKI_COMMON},
            {"role": "system", "content": getattr(prompts, stage)},
            *([{"role": "system", "content": prompts.STRUCTURE_REPAIR}] if repair else []),
            {"role": "user", "content": encode(data)},
            *projected_history,
        ]
        if repair and history:
            messages.append({"role": "user", "content": encode({"repair": repair})})
        return cast(list[dict[str, JsonValue]], messages)

    def saved(self) -> dict:
        return {
            "request_input": self.request,
            "chunks": {k: v.model_dump(mode="json") for k, v in self.chunks.items()},
            "pages": self.pages,
        }

    @classmethod
    def restored(cls, saved: dict) -> WikiContext:
        return cls(
            saved["request_input"],
            {k: WikiChunkRef.model_validate(v) for k, v in saved["chunks"].items()},
            saved["pages"],
        )


def chunk_content(chunk: Chunk, titles: list[str]) -> dict:
    return {
        "section_paths": [
            list(path) for path in dict.fromkeys(tuple(loc.section_path) for loc in chunk.locators)
        ],
        "text": prepare_model_text(chunk.retrieval_text, titles),
    }


def rebind_exact_references(
    page: WikiPage, old: list[Chunk], current: list[Chunk]
) -> tuple[WikiPage, list[int]]:
    """Move a whole statement only when every cited chunk has one exact counterpart."""

    def signature(chunk: Chunk) -> tuple[str, str, str]:
        return (
            chunk.body_text,
            chunk.retrieval_text,
            encode([loc.model_dump(mode="json") for loc in chunk.locators]),
        )

    candidates: dict[tuple[str, str, str], list[Chunk]] = {}
    for chunk in current:
        candidates.setdefault(signature(chunk), []).append(chunk)
    replacements = {}
    for chunk in old:
        matches = candidates.get(signature(chunk), [])
        if len(matches) == 1:
            match = matches[0]
            replacements[chunk.chunk_id] = WikiChunkRef(
                generation_id=match.generation_id, chunk_id=match.chunk_id
            )
    statements, unresolved = [], []
    for index, statement in enumerate(page.statements):
        if all(ref.chunk_id in replacements for ref in statement.chunk_refs):
            statements.append(
                statement.model_copy(
                    update={"chunk_refs": [replacements[r.chunk_id] for r in statement.chunk_refs]}
                )
            )
        else:
            statements.append(statement)
            unresolved.append(index)
    return page.model_copy(update={"statements": statements}), unresolved


def build_reference_rebinding_context(
    entry: SnapshotEntry,
    previous: SnapshotEntry,
    page: WikiPage,
    old: list[Chunk],
    current: list[Chunk],
    unresolved: list[int],
) -> WikiContext:
    old_by_id = {chunk.chunk_id: chunk for chunk in old}
    cited = dict.fromkeys(
        ref.chunk_id for index in unresolved for ref in page.statements[index].chunk_refs
    )
    # Current chunks are complete; only old evidence used by unresolved statements is sent.
    chunks = [*sorted(current, key=lambda c: c.ordinal), *(old_by_id[key] for key in cited)]
    bindings = {
        f"C{i}": WikiChunkRef(generation_id=c.generation_id, chunk_id=c.chunk_id)
        for i, c in enumerate(chunks, 1)
    }
    old_labels = {key: f"C{i}" for i, key in enumerate(cited, len(current) + 1)}
    return WikiContext(
        {
            "target": {
                "title": entry.title,
                "previous_version_notice": source_version_notice(previous),
            },
            "sources": source_metadata([entry]),
            "chunks": {
                f"C{i}": {
                    "source_ref": "D1",
                    **chunk_content(chunk, [entry.title] if entry.title else []),
                }
                for i, chunk in enumerate(chunks, 1)
            },
            "current_chunk_refs": list(bindings)[: len(current)],
            "statements": {
                f"S{index + 1}": {
                    **page.statements[index].model_dump(exclude={"chunk_refs", "related_page_ids"}),
                    "chunk_refs": list(
                        dict.fromkeys(
                            old_labels[r.chunk_id] for r in page.statements[index].chunk_refs
                        )
                    ),
                }
                for index in unresolved
            },
            "response_schema": WikiReferenceRebindingResponse.model_json_schema(),
        },
        bindings,
        {},
    )


def bind_reference_rebinding(
    response: WikiReferenceRebindingResponse, context: WikiContext
) -> dict[str, list[WikiChunkRef]]:
    selected = [statement.statement_ref for statement in response.statements]
    if len(selected) != len(set(selected)) or set(selected) != context.request["statements"].keys():
        raise ValueError("Reference rebinding must select every supplied S exactly once")
    allowed = set(context.request["current_chunk_refs"])
    if any(not set(statement.chunk_refs) <= allowed for statement in response.statements):
        raise ValueError("Reference rebinding must select current-generation C labels")
    return {
        statement.statement_ref: [
            context.chunks[key] for key in dict.fromkeys(statement.chunk_refs)
        ]
        for statement in response.statements
    }


def source_metadata(entries: list[SnapshotEntry]) -> dict:
    return {
        f"D{i}": {
            "title": e.title,
            "issuer": e.policy_metadata.issuer,
            "policy_code": e.policy_metadata.policy_code,
            "version_status": e.policy_metadata.version_status.value,
            "effective_from": model_policy_date(e.policy_metadata.effective_from),
            "effective_to": model_policy_date(e.policy_metadata.effective_to),
        }
        for i, e in enumerate(entries, 1)
    }


def build_context(
    value: WikiBuildInput,
    target: WikiPage,
    *,
    chunks: list[Chunk],
    reports: dict[UUID, ParseReport],
    input_pages: Sequence[WikiPage] = (),
) -> WikiContext:
    """Source analysis includes all text; synthesis includes member pages and C navigation."""
    refs = [WikiChunkRef(generation_id=c.generation_id, chunk_id=c.chunk_id) for c in chunks]
    refs += [r for p in input_pages for s in p.statements for r in s.chunk_refs]
    unique = {(r.generation_id, r.chunk_id): r for r in refs}
    bindings = {f"C{i}": r for i, r in enumerate(unique.values(), 1)}
    if input_pages:
        member_ids = {p.page_id for p in input_pages}
        entries = [e for e in value.snapshot.entries if document_page_id(e) in member_ids]
    else:
        entries = [e for e in value.snapshot.entries if document_page_id(e) == target.page_id]
    sources = source_metadata(entries)
    source_labels = {e.generation_id: f"D{i}" for i, e in enumerate(entries, 1)}
    pages = {f"W{i}": p.page_id for i, p in enumerate(input_pages, 1)}
    page_directory, statements = page_inputs(input_pages, pages)
    for entry in entries:
        for label, key in pages.items():
            if key == document_page_id(entry):
                page_directory[label]["source_ref"] = source_labels[entry.generation_id]
    actual = {(c.generation_id, c.chunk_id): c for c in chunks}
    titles = [e.title for e in entries if e.title]
    if not input_pages:
        for i, entry in enumerate(entries, 1):
            if entry.generation_id in reports:
                report = reports[entry.generation_id]
                sources[f"D{i}"]["diagnostics"] = report.model_dump(
                    mode="json", exclude={"failed_locations", "empty_locations"}
                )
                sources[f"D{i}"]["failed_locations"] = _locations(report.failed_locations)
                sources[f"D{i}"]["empty_locations"] = _locations(report.empty_locations)
    return WikiContext(
        {
            "target": {
                "kind": target.kind,
                "title": target.title,
                **(
                    {"version_notice": source_version_notice(entries[0])}
                    if target.kind == "document"
                    else {}
                ),
            },
            "sources": sources,
            "chunks": {
                label: {
                    "source_ref": source_labels[r.generation_id],
                    **(
                        chunk_content(actual[(r.generation_id, r.chunk_id)], titles)
                        if (r.generation_id, r.chunk_id) in actual
                        else {}
                    ),
                }
                for label, r in bindings.items()
            },
            "pages": page_directory,
            "statements": statements,
            "directory": {
                k: {"title": page_directory[k]["title"], "kind": page_directory[k]["kind"]}
                for k in pages
            },
            "response_schema": WikiStatementsResponse.model_json_schema(),
        },
        bindings,
        pages,
    )


def page_inputs(pages: Sequence[WikiPage], bindings: dict[str, str]) -> tuple[dict, dict]:
    labels = {key: label for label, key in bindings.items()}
    directory, statements = {}, {}
    for page in pages:
        selected = []
        for statement in page.statements:
            label = f"S{len(statements) + 1}"
            selected.append(label)
            statements[label] = statement.model_dump(exclude={"chunk_refs", "related_page_ids"})
        directory[labels[page.page_id]] = {
            "title": page.title,
            "kind": page.kind,
            "statement_refs": selected,
        }
    return directory, statements


def build_classification_context(value: WikiBuildInput, pages: list[WikiPage]) -> WikiContext:
    bindings = {f"W{i}": p.page_id for i, p in enumerate(pages, 1)}
    directory, statements = page_inputs(pages, bindings)
    source_labels = {document_page_id(e): f"D{i}" for i, e in enumerate(value.snapshot.entries, 1)}
    for label, key in bindings.items():
        directory[label]["source_ref"] = source_labels[key]
    return WikiContext(
        {
            "pages": directory,
            "statements": statements,
            "sources": source_metadata(value.snapshot.entries),
            "directory": {
                k: {"title": v["title"], "kind": v["kind"]} for k, v in directory.items()
            },
            "response_schema": WikiThemeClassificationResponse.model_json_schema(),
        },
        {},
        bindings,
    )


def build_overview_context(name: str, pages: list[WikiPage]) -> WikiContext:
    bindings = {f"W{i}": p.page_id for i, p in enumerate(pages, 1)}
    return WikiContext(
        {
            "knowledge_base_name": name,
            "pages": {
                f"W{i}": {
                    "title": p.title,
                    "statements": [
                        s.model_dump(exclude={"chunk_refs", "related_page_ids"})
                        for s in p.statements
                    ],
                }
                for i, p in enumerate(pages, 1)
            },
            "directory": {
                f"W{i}": {"title": p.title, "kind": p.kind} for i, p in enumerate(pages, 1)
            },
            "response_schema": WikiOverviewResponse.model_json_schema(),
        },
        {},
        bindings,
    )


def _locations(locators) -> list[dict]:
    return [
        loc.model_dump(mode="json", exclude={"document_id", "source_hash", "block_ids"})
        for loc in locators
    ]


def bind_statement(statement: WikiStatementResponse, context: WikiContext) -> WikiStatement:
    if (
        not set(statement.chunk_refs) <= context.chunks.keys()
        or not set(statement.related_page_refs) <= context.pages.keys()
    ):
        raise ValueError("References must select supplied C/W labels")
    source = context.request["target"]["kind"] == "document"
    if source and (not statement.chunk_refs or statement.related_page_refs):
        raise ValueError("Source statements require C and no W")
    if not source and (statement.chunk_refs or not statement.related_page_refs):
        raise ValueError("Synthesis statements require W and no C")
    return WikiStatement(
        **statement.model_dump(exclude={"chunk_refs", "related_page_refs", "text"}),
        text=restore_texts(statement.text, context.model_input().get("texts", {})),
        chunk_refs=list(
            {
                (context.chunks[k].generation_id, context.chunks[k].chunk_id): context.chunks[k]
                for k in statement.chunk_refs
            }.values()
        ),
        related_page_ids=list(dict.fromkeys(context.pages[k] for k in statement.related_page_refs)),
    )


def bind_statements(response: WikiStatementsResponse, context: WikiContext) -> list[WikiStatement]:
    return [bind_statement(s, context) for s in response.statements]


def bind_themes(
    response: WikiThemeClassificationResponse, context: WikiContext
) -> list[WikiThemePlan]:
    selected = {ref for theme in response.themes for ref in theme.source_page_refs}
    if not selected <= context.pages.keys():
        raise ValueError("Themes must select supplied source W labels")
    missing = context.pages.keys() - selected
    if missing:
        raise ValueError("Missing source page labels: " + ", ".join(sorted(missing)))
    return [
        WikiThemePlan(
            page_id=f"synthesis-{uuid4()}",
            title=theme.title,
            source_page_ids=list(dict.fromkeys(context.pages[k] for k in theme.source_page_refs)),
        )
        for theme in response.themes
    ]


def bind_overview(response: WikiOverviewResponse, context: WikiContext) -> list[WikiStatement]:
    scenarios = [s.page_ref for s in response.scenarios]
    if len(scenarios) != len(context.pages) or set(scenarios) != context.pages.keys():
        raise ValueError("Scenarios must select each synthesis W exactly once")
    if not set(response.business_context.related_page_refs) <= context.pages.keys():
        raise ValueError("Overview must select supplied synthesis W labels")
    return [
        WikiStatement(
            kind="summary",
            topic="business_context",
            evidence_level="derived",
            text=response.business_context.text,
            chunk_refs=[],
            related_page_ids=list(
                dict.fromkeys(context.pages[k] for k in response.business_context.related_page_refs)
            ),
        ),
        *(
            WikiStatement(
                kind="relation",
                topic="relationship",
                evidence_level="derived",
                text=s.text,
                chunk_refs=[],
                related_page_ids=[context.pages[s.page_ref]],
            )
            for s in response.scenarios
        ),
    ]


def build_review_context(context: WikiContext, page: WikiPage) -> WikiContext:
    reverse = {(r.generation_id, r.chunk_id): k for k, r in context.chunks.items()}
    candidate = {
        f"S{i}": {
            **s.model_dump(exclude={"chunk_refs", "related_page_ids"}),
            "chunk_refs": [reverse[(r.generation_id, r.chunk_id)] for r in s.chunk_refs],
            "related_page_refs": [],
        }
        for i, s in enumerate(page.statements, 1)
    }
    return WikiContext(
        {
            **context.request,
            "candidate": candidate,
            "response_schema": WikiReviewResponse.model_json_schema(),
        },
        context.chunks,
        {},
    )


def build_incremental_classification_context(
    value: WikiBuildInput, new_sources: list[WikiPage], themes: list[WikiPage]
) -> WikiContext:
    ids = {p.page_id for p in new_sources}
    selected = value.model_copy(
        update={
            "snapshot": value.snapshot.model_copy(
                update={
                    "entries": [e for e in value.snapshot.entries if document_page_id(e) in ids]
                }
            )
        }
    )
    context = build_classification_context(selected, new_sources)
    context.request["new_sources"] = context.request.pop("pages")
    existing = {}
    for page in themes:
        label = f"W{len(context.pages) + 1}"
        context.pages[label] = page.page_id
        existing[label] = {"title": page.title, "kind": page.kind}
    context.request["existing_scenarios"] = existing
    context.request["directory"].update(existing)
    context.request["response_schema"] = WikiIncrementalClassificationResponse.model_json_schema()
    return context


def bind_incremental_themes(
    response: WikiIncrementalClassificationResponse,
    context: WikiContext,
    previous: list[WikiThemePlan],
) -> tuple[list[WikiThemePlan], dict[str, list[str]]]:
    sources = set(context.request["new_sources"])
    existing = set(context.request["existing_scenarios"])
    targets = [t.page_ref for t in response.existing_themes]
    if len(set(targets)) != len(targets) or not set(targets) <= existing:
        raise ValueError("Each existing theme must select a distinct synthesis W")
    selected = {
        ref for t in [*response.existing_themes, *response.new_themes] for ref in t.source_page_refs
    }
    if selected != sources:
        raise ValueError("Every new source W must be covered; old source/theme W cannot be members")
    updates = {
        context.pages[t.page_ref]: list(dict.fromkeys(context.pages[r] for r in t.source_page_refs))
        for t in response.existing_themes
    }
    themes = [
        t.model_copy(
            update={
                "source_page_ids": list(
                    dict.fromkeys([*t.source_page_ids, *updates.get(t.page_id, [])])
                )
            }
        )
        for t in previous
    ]
    for item in response.new_themes:
        theme = WikiThemePlan(
            page_id=f"synthesis-{uuid4()}",
            title=item.title,
            source_page_ids=list(dict.fromkeys(context.pages[r] for r in item.source_page_refs)),
        )
        themes.append(theme)
        updates[theme.page_id] = theme.source_page_ids
    return themes, updates


def page_with_references(page: WikiPage, bindings: dict[str, str]) -> dict:
    reverse = {key: label for label, key in bindings.items()}
    return {
        "title": page.title,
        "kind": page.kind,
        "statements": [
            {
                **s.model_dump(exclude={"chunk_refs", "related_page_ids"}),
                "related_page_refs": [reverse[k] for k in s.related_page_ids],
            }
            for s in page.statements
        ],
        "member_page_refs": [reverse[k] for k in page.child_page_ids],
    }


def build_theme_update_context(
    value: WikiBuildInput, previous: WikiPage, members: list[WikiPage], delta: list[WikiPage]
) -> WikiContext:
    context = build_context(value, previous, chunks=[], reports={}, input_pages=delta)
    for page in members:
        if page.page_id not in context.pages.values():
            label = f"W{len(context.pages) + 1}"
            context.pages[label] = page.page_id
            context.request["directory"][label] = {"title": page.title, "kind": page.kind}
    context.request["delta_sources"] = context.request.pop("pages")
    context.request["existing_page"] = page_with_references(previous, context.pages)
    context.request["response_schema"] = WikiThemeUpdateResponse.model_json_schema()
    return context


def bind_theme_update(
    response: WikiThemeUpdateResponse, context: WikiContext, previous: WikiPage
) -> list[WikiStatement]:
    replacements = {s.topic: s for s in bind_statements(response, context)}
    retained = {s.topic: s for s in previous.statements if s.topic not in response.removed_topics}
    retained.update(replacements)
    return [retained[t] for t in TOPICS if t in retained]


def build_overview_update_context(
    name: str, previous: WikiPage, pages: list[WikiPage], previous_ids: set[str], changed: set[str]
) -> WikiContext:
    bindings = {f"W{i}": p.page_id for i, p in enumerate(pages, 1)}
    bodies = {
        label: {
            "title": page.title,
            "statements": [
                s.model_dump(exclude={"chunk_refs", "related_page_ids"}) for s in page.statements
            ],
        }
        for label, page in zip(bindings, pages, strict=True)
        if page.page_id in changed
    }
    return WikiContext(
        {
            "knowledge_base_name": name,
            "existing_overview": page_with_references(previous, bindings),
            "updated_pages": {k: v for k, v in bodies.items() if bindings[k] in previous_ids},
            "new_pages": {k: v for k, v in bodies.items() if bindings[k] not in previous_ids},
            "directory": {
                k: {"title": p.title, "kind": p.kind} for k, p in zip(bindings, pages, strict=True)
            },
            "response_schema": WikiOverviewUpdateResponse.model_json_schema(),
        },
        {},
        bindings,
    )


def bind_overview_update(
    response: WikiOverviewUpdateResponse, context: WikiContext, previous: WikiPage
) -> list[WikiStatement]:
    changed = set(context.request["updated_pages"]) | set(context.request["new_pages"])
    selected = [s.page_ref for s in response.scenarios]
    if len(selected) != len(changed) or set(selected) != changed:
        raise ValueError("Scenarios must select each changed synthesis W exactly once")
    business = response.business_context
    if business is not None and not set(business.related_page_refs) <= context.pages.keys():
        raise ValueError("Overview must select supplied synthesis W labels")
    old_context = next(s for s in previous.statements if s.topic == "business_context")
    new_context = (
        old_context
        if business is None
        else WikiStatement(
            kind="summary",
            topic="business_context",
            evidence_level="derived",
            text=business.text,
            chunk_refs=[],
            related_page_ids=list(
                dict.fromkeys(context.pages[k] for k in business.related_page_refs)
            ),
        )
    )
    scenarios = {s.related_page_ids[0]: s for s in previous.statements if s.topic == "relationship"}
    for item in response.scenarios:
        page_id = context.pages[item.page_ref]
        scenarios[page_id] = WikiStatement(
            kind="relation",
            topic="relationship",
            evidence_level="derived",
            text=item.text,
            chunk_refs=[],
            related_page_ids=[page_id],
        )
    return [new_context, *(scenarios[k] for k in context.pages.values())]
