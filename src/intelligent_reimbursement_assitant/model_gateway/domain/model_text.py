"""Model-readable layout with original chunks preserved for publication."""

import re
from collections import Counter
from collections.abc import Collection, Mapping
from typing import Any

_CELL = r"单元格\[行\d+-\d+,列\d+-\d+,角色=(?:COLUMN|ROW|BOTH|NONE|UNKNOWN)\]:"
_STRUCTURE = re.compile(
    rf"^(?:{_CELL}|\[行组上下文\](?: {_CELL})?|"
    r"\[(?:适用上下文|正文|整表单位|整表条件/脚注|单元格[^\n]+脚注)\])$",
    re.M,
)
_BREAKS = re.compile(r"\n(?:[ \t]*\n)*")
_TITLE_BREAKS = re.compile(r"[ \t]*\n(?:[ \t]*\n)*[ \t]*")

_COMPACT_CELL = re.compile(
    r"单元格\[行(\d+)-(\d+),列(\d+)-(\d+),角色=(COLUMN|ROW|BOTH|NONE|UNKNOWN)\]"
)


def _compact_cell(match: re.Match[str]) -> str:
    row = match[1] if match[1] == match[2] else f"{match[1]}-{match[2]}"
    column = match[3] if match[3] == match[4] else f"{match[3]}-{match[4]}"
    return f"格[r{row},c{column},{match[5]}]"


def prepare_model_text(body_text: str, policy_titles: Collection[str]) -> str:
    text = body_text.replace("\r\n", "\n").replace("\r", "\n")

    # Only the fixed snapshot can establish that a broken title is one name.
    titles = {title.removeprefix("《").removesuffix("》") for title in policy_titles}
    removed: set[int] = set()
    for title in re.finditer(r"《[^》]*》", text):
        if "\n" not in title.group():
            continue
        if _TITLE_BREAKS.sub("", title.group())[1:-1] in titles:
            for breaks in _TITLE_BREAKS.finditer(title.group()):
                removed.update(range(title.start() + breaks.start(), title.start() + breaks.end()))
    if removed:
        text = "".join(c for index, c in enumerate(text) if index not in removed)

    structures = list(_STRUCTURE.finditer(text))
    by_end = {match.end() for match in structures}
    by_start = {match.start() for match in structures}
    output: list[str] = []
    cursor = 0
    structure_index = -1
    for breaks in _BREAKS.finditer(text):
        while (
            structure_index + 1 < len(structures)
            and structures[structure_index + 1].end() <= breaks.start()
        ):
            structure_index += 1
        previous = structures[structure_index] if structure_index >= 0 else None
        table_separator = breaks.start() in by_end or (
            breaks.end() in by_start and previous is not None
        )
        count = breaks.group().count("\n")
        keep = 1 if table_separator else min(count, 2)
        output.append(text[cursor : breaks.start()])
        output.append("\n" * keep)
        cursor = breaks.end()
    output.append(text[cursor:])
    return _COMPACT_CELL.sub(_compact_cell, "".join(output))


def reference_text(text: str, values: Mapping[str, str]) -> str:
    """Replace only complete known texts, longest first; preserve all connecting prose."""
    reverse = {value: reference for reference, value in reversed(list(values.items())) if value}
    if not reverse:
        return text
    pattern = re.compile(
        "|".join(re.escape(value) for value in sorted(reverse, key=len, reverse=True))
    )
    return pattern.sub(lambda match: f"【{reverse[match.group()]}】", text)


def expand_text(text: str, values: Mapping[str, str]) -> str:
    def expand(match: re.Match[str]) -> str:
        reference = match[1]
        if reference not in values:
            raise ValueError("Text contains an unknown reference")
        return expand_text(values[reference], values)

    return re.sub(r"【([A-Z][1-9][0-9]*)】", expand, text)


_FRAGMENT_BOUNDARIES = re.compile(
    r"\n[ \t]*\n(?:[ \t]*\n)*|"
    r"^(?:\[(?:适用上下文|正文|整表单位|整表条件/脚注)\]|"
    r"(?:\[行组上下文\] )?格\[r\d+(?:-\d+)?,c\d+(?:-\d+)?,[A-Z]+\]:|"
    r"\[(?:\[行组上下文\] )?格\[[^\n]+脚注\])",
    re.M,
)


def _worth_sharing(text: str, count: int) -> bool:
    return len(text) >= 12 and "【" not in text and (count - 1) * len(text) > count * 6 + 10


def share_texts(data: dict[str, Any]) -> dict[str, Any]:
    """Share exact repeated fields and structural fragments, preserving every character.

    Callers choose which strings are content; protocol data must stay outside this projection.
    """
    existing: dict[str, str] = data.get("texts", {})

    def reuse(value: Any) -> Any:
        if isinstance(value, str):
            return reference_text(value, existing)
        if isinstance(value, dict):
            return {key: reuse(child) for key, child in value.items()}
        if isinstance(value, list):
            return [reuse(child) for child in value]
        return value

    data = reuse({key: value for key, value in data.items() if key != "texts"})
    strings: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str):
            strings.append(value)
        elif isinstance(value, dict):
            for child in value.values():
                collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)

    collect(data)
    excerpts = {value for value in strings if len(value) >= 12 and "【" not in value}
    # Split only at supplied text and layout boundaries, never by business meaning.
    split: dict[str, list[str]] = {}
    for text in dict.fromkeys(strings):
        boundaries = {0, len(text)}
        for match in _FRAGMENT_BOUNDARIES.finditer(text):
            boundaries.update((match.start(), match.end()))
        # Preserve sharing of complete excerpts supplied in other fields (for
        # example a user fact quoted from a longer message), including overlaps.
        for excerpt in excerpts:
            start = text.find(excerpt)
            while start >= 0:
                boundaries.update((start, start + len(excerpt)))
                start = text.find(excerpt, start + 1)
        ordered = sorted(boundaries)
        # Keep surrounding whitespace in place, so the same content at a field
        # boundary and below a structural marker can share one text binding.
        for left, right in zip(ordered, ordered[1:], strict=False):
            part = text[left:right]
            if part.strip():
                boundaries.update(
                    (left + len(part) - len(part.lstrip()), right - len(part) + len(part.rstrip()))
                )
        ordered = sorted(boundaries)
        split[text] = [text[left:right] for left, right in zip(ordered, ordered[1:], strict=False)]
    # Prefer a whole duplicated field over a series of labels for its paragraphs.
    whole_counts = Counter(strings)
    for text, count in whole_counts.items():
        if count > 1 and _worth_sharing(text, count):
            split[text] = [text]
    counts = Counter(part for text in strings for part in split[text])
    shared = {part for part, count in counts.items() if count > 1 and _worth_sharing(part, count)}
    texts: dict[str, str] = dict(existing)
    references: dict[str, str] = {}
    for text in strings:
        for part in split[text]:
            if part in shared and part not in references:
                reference = f"T{len(texts) + 1}"
                texts[reference] = part
                references[part] = reference

    def project(value: Any) -> Any:
        if isinstance(value, str):
            return "".join(f"【{references[p]}】" if p in references else p for p in split[value])
        if isinstance(value, dict):
            return {key: project(child) for key, child in value.items()}
        if isinstance(value, list):
            return [project(child) for child in value]
        return value

    result = project(data)
    if texts:
        result["texts"] = texts
    return result


def restore_texts(value: Any, bindings: Mapping[str, str]) -> Any:
    """Expand only shared source text; F/V/A selections remain for stage assembly."""
    if isinstance(value, str):

        def restore(match: re.Match[str]) -> str:
            reference = match[1]
            if reference not in bindings:
                raise ValueError("Text must select a supplied shared text reference")
            return bindings[reference]

        return re.sub(r"【(T[1-9][0-9]*)】", restore, value)
    if isinstance(value, dict):
        return {key: restore_texts(child, bindings) for key, child in value.items()}
    if isinstance(value, list):
        return [restore_texts(child, bindings) for child in value]
    return value
