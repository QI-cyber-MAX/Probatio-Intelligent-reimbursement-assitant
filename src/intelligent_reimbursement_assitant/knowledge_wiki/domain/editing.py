"""Bind a complete human edit to one immutable version, at the input boundary."""

from uuid import UUID, uuid5

from intelligent_reimbursement_assitant.knowledge_wiki.domain.errors import WikiEditValidationError
from intelligent_reimbursement_assitant.knowledge_wiki.domain.models import (
    WikiOverviewEdit,
    WikiPackage,
    WikiPage,
    WikiSceneEdit,
    WikiSourcePageEdit,
    WikiStatement,
)


def build_edited_pages(
    base: WikiPackage,
    job_id: UUID,
    source_pages: list[WikiSourcePageEdit],
    scenes: list[WikiSceneEdit],
    overview: WikiOverviewEdit,
    valid_chunks: dict[str, set[tuple[UUID, str]]],
) -> dict[str, WikiPage]:
    errors: list[dict] = []

    def invalid(path: tuple, message: str):
        errors.append({"loc": ("body", *path), "msg": message, "type": "value_error"})

    expected = {key for key, p in base.pages.items() if p.kind == "document"}
    selected = [p.page_id for p in source_pages]
    if len(selected) != len(expected) or set(selected) != expected:
        invalid(("source_pages",), "Each snapshot source page must occur exactly once")
    pages = {}
    for i, item in enumerate(source_pages):
        if item.page_id not in expected:
            invalid(("source_pages", i, "page_id"), "Select a source page from the base version")
            continue
        for j, statement in enumerate(item.statements):
            if statement.related_page_ids:
                invalid(
                    ("source_pages", i, "statements", j, "related_page_ids"),
                    "Source statements cannot reference pages",
                )
            if not statement.chunk_refs:
                invalid(
                    ("source_pages", i, "statements", j, "chunk_refs"), "Select original chunks"
                )
            for k, ref in enumerate(statement.chunk_refs):
                if (ref.generation_id, ref.chunk_id) not in valid_chunks[item.page_id]:
                    invalid(
                        ("source_pages", i, "statements", j, "chunk_refs", k),
                        "Select a real chunk from this source and generation",
                    )
        pages[item.page_id] = base.pages[item.page_id].model_copy(
            update={"statements": item.statements}
        )
    if not scenes:
        invalid(("scenes",), "At least one scene is required")
    scene_bindings: dict[str, str] = {}
    scene_ids = []
    covered = set()
    for i, scene in enumerate(scenes):
        if scene.scene_ref in scene_bindings:
            invalid(("scenes", i, "scene_ref"), "Scene references must be unique")
        key = scene.page_id or f"synthesis-{uuid5(job_id, scene.scene_ref)}"
        if scene.page_id is not None and (
            scene.page_id not in base.pages or base.pages[scene.page_id].kind != "synthesis"
        ):
            invalid(
                ("scenes", i, "page_id"), "Select an existing scene or use null for a new scene"
            )
        if key in scene_ids:
            invalid(("scenes", i, "page_id"), "Scene identities must be unique")
        scene_ids.append(key)
        scene_bindings[scene.scene_ref] = key
        members = set(scene.source_page_ids)
        if not members <= expected or len(members) != len(scene.source_page_ids):
            invalid(
                ("scenes", i, "source_page_ids"),
                "Select distinct source pages from the base version",
            )
        covered.update(members)
        for j, statement in enumerate(scene.statements):
            if statement.chunk_refs:
                invalid(
                    ("scenes", i, "statements", j, "chunk_refs"),
                    "Synthesis statements cannot reference chunks",
                )
            if not statement.related_page_ids or not set(statement.related_page_ids) <= members:
                invalid(
                    ("scenes", i, "statements", j, "related_page_ids"),
                    "Select supporting member source pages",
                )
        pages[key] = WikiPage(
            page_id=key,
            kind="synthesis",
            title=scene.title,
            statements=scene.statements,
            child_page_ids=scene.source_page_ids,
        )
    if covered != expected:
        invalid(("scenes",), "Every source must belong to at least one scene")
    refs = overview.business_context.related_scene_refs
    if not set(refs) <= scene_bindings.keys():
        invalid(
            ("overview", "business_context", "related_scene_refs"), "Select retained or new scenes"
        )
    scenario_refs = [s.scene_ref for s in overview.scenarios]
    if len(scenario_refs) != len(scenes) or set(scenario_refs) != scene_bindings.keys():
        invalid(("overview", "scenarios"), "Each scene must have exactly one overview entry")
    if errors:
        raise WikiEditValidationError(errors)
    descriptions = {s.scene_ref: s.text for s in overview.scenarios}
    statements = [
        WikiStatement(
            kind="summary",
            topic="business_context",
            evidence_level="derived",
            text=overview.business_context.text,
            chunk_refs=[],
            related_page_ids=list(dict.fromkeys(scene_bindings[r] for r in refs)),
        ),
        *(
            WikiStatement(
                kind="relation",
                topic="relationship",
                evidence_level="derived",
                text=descriptions[s.scene_ref],
                chunk_refs=[],
                related_page_ids=[scene_bindings[s.scene_ref]],
            )
            for s in scenes
        ),
    ]
    pages["overview"] = base.pages["overview"].model_copy(
        update={"statements": statements, "child_page_ids": scene_ids}
    )
    return pages
