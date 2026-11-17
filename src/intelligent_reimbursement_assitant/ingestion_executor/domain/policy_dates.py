"""Extract explicit self-referential effective dates from parsed source structure."""

import re
import unicodedata
from datetime import date

from intelligent_reimbursement_assitant.contracts.models import SourceLocator
from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    PolicyDate,
    PolicyDateSource,
)
from intelligent_reimbursement_assitant.parser.domain.models import (
    DocumentBlock,
    ParsedDocument,
    TableBlock,
)

_NUMBER = r"[0-9〇零一二三四五六七八九十两]+"
_DATE = rf"(?<![0-9〇零一二三四五六七八九十两])({_NUMBER})年({_NUMBER})月(?:({_NUMBER})日)?(?![0-9〇零一二三四五六七八九十两])"
_DATE_RE = re.compile(_DATE)
_SELF = r"本(?:办法|规定|细则|意见|通知|制度)"
_ACTION = r"(?:施行|生效|执行|实施|实行)"
_ISSUANCE = r"(?:印发|发布|公布|颁布|颁发|下发)"
# A parsed paragraph (or table cell) may contain several complete sentences.
_DIRECT = re.compile(rf"{_SELF}.*?自({_DATE})(?:起|日起)?{_ACTION}")
_REFERENCE = re.compile(rf"{_SELF}.*?自({_ISSUANCE})之日起{_ACTION}")
_APPROVAL = re.compile(
    rf"{_SELF}经({_DATE}[^。；]*?会议(?:审议)?通过)[，,。；;]?自(?:学校)?通过之日起{_ACTION}"
)
_APPROVAL_REFERENCE = re.compile(rf"{_SELF}.*?自(?:学校)?通过之日起{_ACTION}")
_APPROVAL_STAMP = re.compile(rf"\(?({_DATE})[^。；《》]*?会议(?:审议)?通过\)?")
_INSTITUTION = r"[\u4e00-\u9fff（）()·、]+(?:部|厅|局|委|委员会|大学|办公室|研究院|基金会)"
_SIGNATURE = re.compile(rf"(?:{_INSTITUTION})+({_DATE})?$")
_BODY_START = re.compile(r"^(?:第[一1]章|第[一1]条)")


def _view(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def _number(text: str) -> int:
    if text.isdecimal():
        return int(text)
    digits = {c: n for n, c in enumerate("零一二三四五六七八九")}
    digits.update({"〇": 0, "两": 2})
    if "十" in text:
        tens, ones = text.split("十")
        return digits.get(tens, 1) * 10 + digits.get(ones, 0)
    return int("".join(str(digits[c]) for c in text))


def _dates(text: str) -> list[str]:
    result = []
    for match in _DATE_RE.finditer(text):
        year, month, day = match.groups()
        try:
            parsed = date(_number(year), _number(month), _number(day) if day else 1)
        except ValueError, KeyError:
            continue
        result.append(parsed.isoformat() if day else parsed.isoformat()[:7])
    return result


def extract_effective_date(parsed: ParsedDocument) -> PolicyDate | None:
    candidates: dict[str, list[SourceLocator]] = {}

    def add(text: str, locators: list[SourceLocator]) -> None:
        for value in _dates(text):
            stored = candidates.setdefault(value, [])
            for locator in locators:
                if locator not in stored:
                    stored.append(locator)

    # Empty paragraphs do not interrupt adjacent signatures or split clauses.
    blocks = [b for b in parsed.blocks if isinstance(b, TableBlock) or _view(b.text)]
    texts = [(b, _view(b.text)) for b in blocks if isinstance(b, DocumentBlock)]
    body_start = next((i for i, (_, t) in enumerate(texts) if _BODY_START.match(t)), len(texts))
    references: list[SourceLocator] = []
    publication_references: list[SourceLocator] = []
    approval_references: list[SourceLocator] = []
    for i, (block, text) in enumerate(texts):
        clause = text
        locators = [block.locator]
        j = i + 1
        while j < len(texts) and re.search(rf"{_SELF}[^。；;！？]*$", clause):
            following, following_text = texts[j]
            previous = texts[j - 1][0]
            previous_end = previous.locator.page_end or previous.locator.page_start
            if (
                following.parent_id != block.parent_id
                or following.locator.section_path != block.locator.section_path
                or (
                    previous_end is not None
                    and following.locator.page_start is not None
                    and following.locator.page_start - previous_end not in (0, 1)
                )
                or isinstance(blocks[blocks.index(previous) + 1], TableBlock)
            ):
                break
            clause += following_text
            locators.append(following.locator)
            j += 1
        for match in _DIRECT.finditer(clause):
            add(match.group(1), locators)
        for match in _APPROVAL.finditer(clause):
            add(match.group(1), locators)
        if i < body_start:
            match = re.fullmatch(rf"生效日期[:：]?({_DATE})", text)
            if match:
                add(match.group(1), [block.locator])
        for match in _REFERENCE.finditer(clause):
            references.extend(locator for locator in locators if locator not in references)
            if match.group(1) != "印发":
                publication_references.extend(locators)
        if _APPROVAL_REFERENCE.search(clause):
            approval_references.extend(
                locator for locator in locators if locator not in approval_references
            )

    # Cell prose has the same explicit rules; metadata labels belong to the header.
    body_block = texts[body_start][0] if body_start < len(texts) else None
    header = True
    for block in blocks:
        if block == body_block:
            header = False
        if isinstance(block, TableBlock):
            for label in block.cells:
                cell_text = _view(label.text)
                cell_locator = block.locator.model_copy(
                    update={
                        "cell_range": f"R{label.row_index + 1}C{label.column_index + 1}:R{label.row_index + label.row_span}C{label.column_index + label.column_span}"
                    }
                )
                for rule in (_DIRECT, _APPROVAL):
                    for match in rule.finditer(cell_text):
                        add(match.group(1), [cell_locator])
                for match in _REFERENCE.finditer(cell_text):
                    references.append(cell_locator)
                    if match.group(1) != "印发":
                        publication_references.append(cell_locator)
                if _APPROVAL_REFERENCE.search(cell_text):
                    approval_references.append(cell_locator)
                if not header:
                    continue
                inline = re.fullmatch(rf"生效日期[:：]?({_DATE})", cell_text)
                if inline:
                    add(inline.group(1), [cell_locator])
                if _view(label.text).rstrip(":：") != "生效日期":
                    continue
                for value in block.cells:
                    if (
                        value.row_index == label.row_index
                        and value.column_index == label.column_index + label.column_span
                    ):
                        if _DATE_RE.fullmatch(_view(value.text)):
                            locator = block.locator.model_copy(
                                update={
                                    "cell_range": f"R{label.row_index + 1}C{label.column_index + 1}:R{value.row_index + value.row_span}C{value.column_index + value.column_span}"
                                }
                            )
                            add(_view(value.text), [locator])

    if approval_references:
        # A dated meeting stamp before the body identifies this source's approval;
        # notice signatures and print dates belong to separate issuance events.
        for block, text in texts[:body_start]:
            match = _APPROVAL_STAMP.fullmatch(text)
            if match:
                add(match.group(), approval_references + [block.locator])

    if references:
        explicit: list[tuple[str, list[SourceLocator]]] = []
        for block, text in texts:
            match = re.fullmatch(rf"{_ISSUANCE}日期[:：]?({_DATE})", text)
            if match:
                explicit.append((match.group(1), [block.locator]))
            else:
                match = re.search(rf"({_DATE}){_ISSUANCE}$", text)
                if match:
                    explicit.append((match.group(1), [block.locator]))
        # The notice signature must occur after an explicit issuing/forwarding notice.
        notice_seen = False
        signatures: list[tuple[str, list[SourceLocator]]] = []
        for i, (block, text) in enumerate(texts[:body_start]):
            notice_seen = notice_seen or bool(re.search(rf"现[^。]*?(?:{_ISSUANCE}|转发)", text))
            if not notice_seen:
                continue
            match = _SIGNATURE.fullmatch(text)
            if match and _dates(text):
                signatures.append((text, [block.locator]))
            elif i and _DATE_RE.fullmatch(text) and _SIGNATURE.fullmatch(texts[i - 1][1]):
                signatures.append((text, [texts[i - 1][0].locator, block.locator]))
        if publication_references and texts:
            last, last_text = texts[-1]
            if _SIGNATURE.fullmatch(last_text) and _dates(last_text):
                signatures.append((last_text, [last.locator]))
            elif (
                len(texts) > 1
                and _DATE_RE.fullmatch(last_text)
                and _SIGNATURE.fullmatch(texts[-2][1])
            ):
                signatures.append((last_text, [texts[-2][0].locator, last.locator]))
        for text, locators in explicit + signatures:
            add(text, references + locators)
    if len(candidates) != 1:
        return None
    value, locators = next(iter(candidates.items()))
    return PolicyDate(value=value, source=PolicyDateSource.DOCUMENT, locators=locators)
