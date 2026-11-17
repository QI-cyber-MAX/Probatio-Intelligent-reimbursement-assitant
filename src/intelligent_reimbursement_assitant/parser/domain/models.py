"""Parser output contracts; validation runs at conversion and persistence boundaries."""

from enum import StrEnum
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from intelligent_reimbursement_assitant.contracts.models import SourceLocator


class BlockKind(StrEnum):
    HEADING = "HEADING"
    PARAGRAPH = "PARAGRAPH"
    LIST = "LIST"
    FOOTNOTE = "FOOTNOTE"
    TABLE = "TABLE"


class HeaderRole(StrEnum):
    COLUMN = "COLUMN"
    ROW = "ROW"
    BOTH = "BOTH"
    NONE = "NONE"
    UNKNOWN = "UNKNOWN"


class CoverageStatus(StrEnum):
    COMPLETE = "COMPLETE"
    PARTIAL = "PARTIAL"
    UNKNOWN = "UNKNOWN"


class DocumentBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    block_id: str
    kind: Literal[BlockKind.HEADING, BlockKind.PARAGRAPH, BlockKind.LIST, BlockKind.FOOTNOTE]
    text: str
    parent_id: str | None
    locator: SourceLocator


class TableCell(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    row_index: int = Field(ge=0)
    column_index: int = Field(ge=0)
    row_span: int = Field(ge=1)
    column_span: int = Field(ge=1)
    text: str
    header_role: HeaderRole
    footnote_block_ids: list[str] = Field(default_factory=list)


class TableBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    block_id: str
    kind: Literal[BlockKind.TABLE] = BlockKind.TABLE
    parent_id: str | None
    locator: SourceLocator
    cells: list[TableCell]
    unit_block_ids: list[str] = Field(default_factory=list)
    footnote_block_ids: list[str] = Field(default_factory=list)


class ParseReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    page_count: int | None = Field(default=None, ge=1)
    failed_locations: list[SourceLocator] = Field(default_factory=list)
    empty_locations: list[SourceLocator] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    coverage_status: CoverageStatus
    table_count: int = Field(ge=0)


class ParsedDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    document_id: UUID
    source_hash: str
    blocks: list[Annotated[DocumentBlock | TableBlock, Field(discriminator="kind")]]
    parse_report: ParseReport

    @model_validator(mode="after")
    def validate_structure(self) -> Self:
        by_id = {block.block_id: block for block in self.blocks}
        if len(by_id) != len(self.blocks):
            raise ValueError("duplicate block identity")
        locators = [b.locator for b in self.blocks]
        locators += self.parse_report.failed_locations + self.parse_report.empty_locations
        for locator in locators:
            if (locator.document_id, locator.source_hash) != (self.document_id, self.source_hash):
                raise ValueError("locator belongs to another source")
            if not set(locator.block_ids).issubset(by_id):
                raise ValueError("locator refers to an absent block")
        for block in self.blocks:
            ancestors = {block.block_id}
            parent = block.parent_id
            while parent is not None:
                if parent not in by_id or parent in ancestors:
                    raise ValueError("invalid parent reference")
                ancestors.add(parent)
                parent = by_id[parent].parent_id
            if isinstance(block, TableBlock):
                refs = block.unit_block_ids + block.footnote_block_ids
                active: list[TableCell] = []
                for cell in sorted(block.cells, key=lambda c: c.row_index):
                    active = [c for c in active if c.row_index + c.row_span > cell.row_index]
                    if any(
                        c.column_index < cell.column_index + cell.column_span
                        and cell.column_index < c.column_index + c.column_span
                        for c in active
                    ):
                        raise ValueError("overlapping table cells")
                    active.append(cell)
                    refs += cell.footnote_block_ids
                if any(ref not in by_id or isinstance(by_id[ref], TableBlock) for ref in refs):
                    raise ValueError("table reference must target a retained text block")
        return self
