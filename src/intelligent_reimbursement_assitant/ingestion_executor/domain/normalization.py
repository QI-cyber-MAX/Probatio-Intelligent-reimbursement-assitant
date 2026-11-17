"""Text normalization; structure and source locations belong to parsed documents."""

import unicodedata
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from intelligent_reimbursement_assitant.contracts.models import ArtifactRef
from intelligent_reimbursement_assitant.parser.domain.models import (
    DocumentBlock,
    ParsedDocument,
    TableBlock,
)


class NormalizedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    blocks: list[Annotated[DocumentBlock | TableBlock, Field(discriminator="kind")]]
    original_structure_ref: ArtifactRef


def normalize_text(text: str) -> str:
    return unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))


def normalize_document(parsed: ParsedDocument, parsed_ref: ArtifactRef) -> NormalizedDocument:
    blocks: list[DocumentBlock | TableBlock] = []
    for block in parsed.blocks:
        if isinstance(block, DocumentBlock):
            blocks.append(block.model_copy(update={"text": normalize_text(block.text)}))
        else:
            cells = [
                cell.model_copy(update={"text": normalize_text(cell.text)}) for cell in block.cells
            ]
            blocks.append(block.model_copy(update={"cells": cells}))
    return NormalizedDocument(blocks=blocks, original_structure_ref=parsed_ref)
