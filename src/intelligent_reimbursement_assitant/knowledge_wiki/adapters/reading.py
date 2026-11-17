from functools import cache
from uuid import UUID

from intelligent_reimbursement_assitant.contracts.models import ArtifactRef
from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    Chunk,
    SnapshotEntry,
)
from intelligent_reimbursement_assitant.errors.codes import ErrorCode as Code
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.knowledge_wiki.adapters.artifacts import WikiArtifactReader
from intelligent_reimbursement_assitant.knowledge_wiki.domain.compilation import (
    document_page_id,
)
from intelligent_reimbursement_assitant.knowledge_wiki.domain.models import TOPICS
from intelligent_reimbursement_assitant.scanner.ports.artifact_store import ArtifactStorePort


class LocalWikiReader:
    def __init__(self, store: ArtifactStorePort):
        self.artifacts = WikiArtifactReader(store)

    def project_page(
        self,
        page: dict,
        wiki_bindings: dict[str, str],
        chunk_bindings: dict[str, str],
        source_refs: dict[UUID, str],
    ) -> tuple[dict, dict[str, str], dict[str, str]]:
        wiki, chunks = dict(wiki_bindings), dict(chunk_bindings)
        for key in [page["page_ref"], *page["directory"]]:
            if key not in wiki.values():
                wiki[f"W{len(wiki) + 1}"] = key
        for key in page["chunks"]:
            if key not in chunks.values():
                chunks[f"C{len(chunks) + 1}"] = key
        w = {key: label for label, key in wiki.items()}
        c = {key: label for label, key in chunks.items()}

        def metadata(source):
            return {
                "revision_ref": source_refs[source["generation_id"]],
                **{key: value for key, value in source.items() if key != "generation_id"},
            }

        return (
            {
                **{key: value for key, value in page.items() if key != "page_members"},
                "page_ref": w[page["page_ref"]],
                "sections": {
                    title: [
                        {
                            **s,
                            "chunk_refs": [c[k] for k in s["chunk_refs"]],
                            "related_page_refs": [w[k] for k in s["related_page_refs"]],
                        }
                        for s in statements
                    ]
                    for title, statements in page["sections"].items()
                },
                "sources": [metadata(source) for source in page["sources"]],
                "directory": {
                    w[key]: {
                        **entry,
                        **(
                            {"sources": [metadata(s) for s in entry["sources"]]}
                            if "sources" in entry
                            else {}
                        ),
                    }
                    for key, entry in page["directory"].items()
                },
                "chunks": {
                    c[key]: {**entry, "page_ref": w[entry["page_ref"]]}
                    for key, entry in page["chunks"].items()
                },
            },
            wiki,
            chunks,
        )

    def load_page(
        self,
        ref: ArtifactRef,
        kb_id: UUID,
        page_id: str,
        entries: list[SnapshotEntry],
    ) -> dict:
        package = self.artifacts.read_package(ref, kb_id)
        if page_id not in package.pages:
            raise ExecutionError(Code.WIKI_ARTIFACT_ERROR, "PERMANENT", "Wiki page unavailable")
        allowed = {e.generation_id for e in entries}
        sources = {document_page_id(e): e for e in package.snapshot.entries}

        @cache
        def dependencies(key: str) -> frozenset[UUID]:
            page = package.pages[key]
            if page.kind == "document":
                return frozenset([sources[key].generation_id])
            return frozenset(
                g
                for statement in page.statements
                for related in statement.related_page_ids
                for g in dependencies(related)
            )

        @cache
        def visible(key: str) -> bool:
            page = package.pages[key]
            if page.kind == "document":
                return sources[key].generation_id in allowed
            return any(visible(child) for child in page.child_page_ids)

        @cache
        def statements_for(key: str):
            page = package.pages[key]
            if page.kind == "document":
                return page.statements if dependencies(key) <= allowed else []
            return [
                s
                for s in page.statements
                if all(visible(k) and dependencies(k) <= allowed for k in s.related_page_ids)
            ]

        def metadata(key):
            entry = sources[key]
            report = package.parse_reports[entry.generation_id]
            return {
                "generation_id": entry.generation_id,
                "coverage_status": report.coverage_status,
                "failed_location_count": len(report.failed_locations),
                "warning_count": len(report.warnings),
            }

        page = package.pages[page_id]
        statements = statements_for(page_id)
        links = [k for k in page.child_page_ids if visible(k)]
        chunks = {}
        w = {key: key for key in [page_id, *links]}
        refs = {(r.generation_id, r.chunk_id): r for s in statements for r in s.chunk_refs}
        navigation = {}
        if page.kind == "document" and visible(page_id):
            entry = sources[page_id]
            batch = self.artifacts.read_chunks(entry, package.artifacts[entry.generation_id])
            index = {c.chunk_id: c for c in batch.chunks}
            for r in refs.values():
                if r.chunk_id not in index:
                    raise ExecutionError(
                        Code.WIKI_SOURCE_UNAVAILABLE,
                        "PERMANENT",
                        "Wiki reference has no original chunk",
                    )
                if r.chunk_id not in chunks.values():
                    chunks[r.chunk_id] = r.chunk_id
                label = next(k for k, v in chunks.items() if v == r.chunk_id)
                chunk = index[r.chunk_id]
                locations = []
                for loc in chunk.locators:
                    location = loc.model_dump(
                        mode="json", exclude={"document_id", "source_hash", "block_ids"}
                    )
                    if location not in locations:
                        locations.append(location)
                navigation[label] = {
                    "content_kind": "wiki_reference",
                    "page_ref": w[page_id],
                    "section_paths": [
                        list(p)
                        for p in dict.fromkeys(tuple(loc.section_path) for loc in chunk.locators)
                    ],
                    "locations": locations,
                }
        c = {key: label for label, key in chunks.items()}

        def projection(statement):
            return {
                "kind": statement.kind,
                "evidence_level": statement.evidence_level,
                "text": statement.text,
                "chunk_refs": [c[r.chunk_id] for r in statement.chunk_refs],
                "related_page_refs": [w[k] for k in statement.related_page_ids if k in links],
            }

        if page.kind == "overview":
            sections = {}
            for statement in statements:
                title = (
                    TOPICS["business_context"]
                    if statement.topic == "business_context"
                    else package.pages[statement.related_page_ids[0]].title
                )
                sections.setdefault(title, []).append(projection(statement))
        else:
            sections = {
                title: [projection(s) for s in statements if s.topic == topic]
                for topic, title in TOPICS.items()
                if any(s.topic == topic for s in statements)
            }
        return {
            "page_ref": w[page_id],
            "page_members": {
                key: p.child_page_ids for key, p in package.pages.items() if p.kind == "synthesis"
            },
            "kind": page.kind,
            "title": page.title,
            "sections": sections,
            "sources": [metadata(page_id)] if page.kind == "document" and visible(page_id) else [],
            "directory": {
                w[k]: {
                    "kind": package.pages[k].kind,
                    "title": package.pages[k].title,
                    "topics": [
                        topic
                        for topic in TOPICS
                        if any(s.topic == topic for s in statements_for(k))
                    ],
                    **({"sources": [metadata(k)]} if package.pages[k].kind == "document" else {}),
                }
                for k in links
            },
            "chunks": navigation,
        }

    def read_source_chunks(
        self,
        ref: ArtifactRef,
        kb_id: UUID,
        chunk_ids: list[str],
        entries: list[SnapshotEntry],
        neighbor_radius: int,
    ) -> list[Chunk]:
        package = self.artifacts.read_package(ref, kb_id)
        refs = {
            r.chunk_id: r
            for p in package.pages.values()
            for s in p.statements
            for r in s.chunk_refs
        }
        if not set(chunk_ids) <= refs.keys():
            raise ExecutionError(
                Code.WIKI_SOURCE_UNAVAILABLE, "PERMANENT", "Wiki chunk reference unavailable"
            )
        return self.artifacts.read_source_chunks(
            [refs[k] for k in chunk_ids], entries, package.artifacts, neighbor_radius
        )
