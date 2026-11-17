"""Read-only file format, structure and recursive resource checks on byte streams."""

import posixpath
import struct
import zlib
from copy import copy
from tempfile import SpooledTemporaryFile
from typing import BinaryIO, cast
from urllib.parse import unquote, urlsplit
from xml.etree.ElementTree import Element, ParseError, TreeBuilder
from zipfile import BadZipFile, ZipFile

import olefile
from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import XMLParser
from olefile.olefile import OleFileError
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.scanner.domain.errors import UploadFailure
from intelligent_reimbursement_assitant.scanner.domain.models import (
    DOC,
    DOCX,
    PDF,
    XLSX,
    UploadPolicy,
)

_CT = "http://schemas.openxmlformats.org/package/2006/content-types"
_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
_OFFICE_RELS = {
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument",
    "http://purl.oclc.org/ooxml/officeDocument/relationships/officeDocument",
}
_MAIN_TYPES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml": DOCX,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml": XLSX,
}
_MAIN_ROOTS = {
    DOCX: {
        "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document",
        "{http://purl.oclc.org/ooxml/wordprocessingml/main}document",
    },
    XLSX: {
        "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}workbook",
        "{http://purl.oclc.org/ooxml/spreadsheetml/main}workbook",
    },
}


def _invalid() -> UploadFailure:
    return UploadFailure(ErrorCode.INVALID_FILE_STRUCTURE, "文件结构损坏或缺少格式识别信息。")


def _limit() -> UploadFailure:
    return UploadFailure(ErrorCode.UPLOAD_RESOURCE_LIMIT, "文件容器或格式识别超过资源限制。")


def _rejected() -> UploadFailure:
    return UploadFailure(ErrorCode.FILE_POLICY_REJECTED, "不接收加密文件或含宏的文件。")


def _zip_preflight(source: BinaryIO, policy: UploadPolicy) -> None:
    """Check the real central directory before ZipFile constructs its entry list."""
    source.seek(0, 2)
    size = source.tell()
    source.seek(max(0, size - 65557))
    tail = source.read(65557)
    index = tail.rfind(b"PK\x05\x06")
    if index < 0 or len(tail) - index < 22:
        raise _invalid()
    _, disk, cd_disk, disk_count, count, cd_size, cd_offset, comment_size = struct.unpack_from(
        "<4s4H2IH", tail, index
    )
    if disk or cd_disk or disk_count != count or len(tail) - index != 22 + comment_size:
        raise _invalid()
    end_offset = size - len(tail) + index
    if count == 65535 or cd_size == 0xFFFFFFFF or cd_offset == 0xFFFFFFFF:
        source.seek(end_offset - 20)
        locator = source.read(20)
        if len(locator) != 20:
            raise _invalid()
        signature, zip64_disk, zip64_offset, disks = struct.unpack("<4sIQI", locator)
        if signature != b"PK\x06\x07" or zip64_disk != 0 or disks != 1:
            raise _invalid()
        source.seek(zip64_offset)
        fixed64 = source.read(56)
        if len(fixed64) != 56:
            raise _invalid()
        signature, record_size, _, _, disk, cd_disk, disk_count, count, cd_size, cd_offset = (
            struct.unpack("<4sQ2H2I4Q", fixed64)
        )
        if signature != b"PK\x06\x06" or record_size < 44 or disk or cd_disk or disk_count != count:
            raise _invalid()
        if record_size > policy.max_container_directory_bytes:
            raise _limit()
        if zip64_offset + record_size + 12 != end_offset - 20:
            raise _invalid()
        end_offset = zip64_offset
    if count > policy.max_container_entries or cd_size > policy.max_container_directory_bytes:
        raise _limit()
    if cd_offset + cd_size != end_offset:
        raise _invalid()
    source.seek(cd_offset)
    for _ in range(count):
        fixed = source.read(46)
        if len(fixed) != 46 or fixed[:4] != b"PK\x01\x02":
            raise _invalid()
        name_size, extra_size, entry_comment_size = struct.unpack_from("<3H", fixed, 28)
        source.seek(name_size + extra_size + entry_comment_size, 1)
        if source.tell() > cd_offset + cd_size:
            raise _invalid()
    if source.tell() != cd_offset + cd_size:
        raise _invalid()
    source.seek(0)


def _part_name(name: str) -> str:
    decoded = unquote(name)
    parts = decoded.rstrip("/").split("/")
    if (
        not decoded
        or decoded.startswith("/")
        or "\\" in decoded
        or any(part in {"", ".", ".."} or ":" in part for part in parts)
        or any(ord(char) < 32 for char in decoded)
    ):
        raise _invalid()
    return decoded


class _BoundedTreeBuilder(TreeBuilder):
    """Limit XML complexity before building an excessive element tree."""

    def __init__(self):
        super().__init__()
        self.depth = 0
        self.count = 0

    def start(self, tag: str, attrs: dict[str, str]) -> Element:
        self.depth += 1
        self.count += 1
        if self.depth > 128 or self.count > 500_000:
            raise _limit()
        return super().start(tag, attrs)

    def end(self, tag: str) -> Element:
        self.depth -= 1
        return super().end(tag)


def _xml(package: ZipFile, name: str, limit: int) -> Element:
    parser = XMLParser(
        target=_BoundedTreeBuilder(), forbid_dtd=True, forbid_entities=True, forbid_external=True
    )
    with package.open(name) as member:
        size = 0
        while block := member.read(min(65536, limit + 1 - size)):
            size += len(block)
            if size > limit:
                raise _limit()
            parser.feed(block)
    return parser.close()


def _probe_ooxml(source: BinaryIO, policy: UploadPolicy) -> str:
    _zip_preflight(source, policy)
    with ZipFile(source) as package:
        entries = package.infolist()
        if len(entries) > policy.max_container_entries:
            raise _limit()
        parts: dict[str, str] = {}
        expanded = 0
        for entry in entries:
            name = _part_name(entry.filename)
            if name in parts:
                raise _invalid()
            parts[name] = entry.filename
            if entry.flag_bits & 1:
                raise _rejected()
            if "vbaproject" in name.lower() or name.lower().endswith("vba.bin"):
                raise _rejected()
            expanded += entry.file_size
            if (
                entry.file_size > policy.max_container_member_bytes
                or expanded > policy.max_container_expanded_bytes
            ):
                raise _limit()
        if "[Content_Types].xml" not in parts or "_rels/.rels" not in parts:
            raise _invalid()
        types = _xml(package, parts["[Content_Types].xml"], policy.max_container_directory_bytes)
        relations = _xml(package, parts["_rels/.rels"], policy.max_container_directory_bytes)
        if types.tag != f"{{{_CT}}}Types" or relations.tag != f"{{{_REL}}}Relationships":
            raise _invalid()
        overrides: dict[str, str] = {}
        for item in types:
            content_type = item.get("ContentType", "")
            if "macroenabled" in content_type.lower() or "vbaproject" in content_type.lower():
                raise _rejected()
            if item.tag == f"{{{_CT}}}Override":
                part = item.get("PartName", "")
                if not part.startswith("/"):
                    raise _invalid()
                name = _part_name(part[1:])
                if name in overrides:
                    raise _invalid()
                overrides[name] = content_type
        mains = [item for item in relations if item.get("Type") in _OFFICE_RELS]
        if len(mains) != 1 or mains[0].get("TargetMode", "Internal") != "Internal":
            raise _invalid()
        target = mains[0].get("Target", "")
        uri = urlsplit(target)
        if uri.scheme or uri.netloc or uri.query or uri.fragment:
            raise _invalid()
        main = _part_name(uri.path.removeprefix("/"))
        media = _MAIN_TYPES.get(overrides.get(main, ""))
        if media is None or main not in parts:
            raise _invalid()
        root = _xml(package, parts[main], policy.max_container_member_bytes)
        if root.tag not in _MAIN_ROOTS[media]:
            raise _invalid()
        return media


def _ole_preflight(source: BinaryIO, policy: UploadPolicy) -> None:
    """Bound CFB directory allocation before OleFileIO loads directory entries."""
    source.seek(0, 2)
    file_size = source.tell()
    source.seek(0)
    header = source.read(512)
    if len(header) != 512:
        raise _invalid()
    shift = struct.unpack_from("<H", header, 30)[0]
    if shift not in {9, 12}:
        raise _invalid()
    sector_size = 1 << shift
    sector_count = file_size // sector_size - 1

    def read_sector(sid: int) -> bytes:
        if sid >= sector_count:
            raise _invalid()
        source.seek((sid + 1) * sector_size)
        result = source.read(sector_size)
        if len(result) != sector_size:
            raise _invalid()
        return result

    fat_count = struct.unpack_from("<I", header, 44)[0]
    difat = list(struct.unpack_from("<109I", header, 76))
    next_difat, difat_count = struct.unpack_from("<2I", header, 68)
    if difat_count > sector_count or fat_count > sector_count:
        raise _invalid()
    if difat_count * sector_size > policy.max_container_directory_bytes:
        raise _limit()
    seen: set[int] = set()
    for _ in range(difat_count):
        if next_difat in seen:
            raise _invalid()
        seen.add(next_difat)
        data = read_sector(next_difat)
        difat.extend(struct.unpack_from(f"<{sector_size // 4 - 1}I", data))
        next_difat = struct.unpack_from("<I", data, sector_size - 4)[0]
    fat_sectors = [sid for sid in difat if sid != 0xFFFFFFFF]
    if len(fat_sectors) != fat_count:
        raise _invalid()
    next_dir = struct.unpack_from("<I", header, 48)[0]
    seen.clear()
    slots = 0
    while next_dir != 0xFFFFFFFE:
        if next_dir in seen:
            raise _invalid()
        seen.add(next_dir)
        read_sector(next_dir)
        slots += sector_size // 128
        if (
            slots > policy.max_container_entries
            or len(seen) * sector_size > policy.max_container_directory_bytes
        ):
            raise _limit()
        fat_index, entry_index = divmod(next_dir, sector_size // 4)
        if fat_index >= len(fat_sectors):
            raise _invalid()
        fat = read_sector(fat_sectors[fat_index])
        next_dir = struct.unpack_from("<I", fat, entry_index * 4)[0]
    source.seek(0)


def _probe_doc(source: BinaryIO, policy: UploadPolicy) -> str:
    _ole_preflight(source, policy)
    with olefile.OleFileIO(source, raise_defects=olefile.DEFECT_INCORRECT) as document:
        names = document.listdir(streams=True, storages=True)
        if len(names) > policy.max_container_entries:
            raise _limit()
        for name in names:
            if any(
                part.lower()
                in {"encryptioninfo", "encryptedpackage", "macros", "vba", "_vba_project"}
                for part in name
            ):
                raise _rejected()
        if not document.exists("WordDocument"):
            raise UploadFailure(ErrorCode.UNSUPPORTED_FILE_TYPE, "OLE 容器不是受支持的 DOC 文件。")
        size = document.get_size("WordDocument")
        if size > policy.max_container_member_bytes:
            raise _limit()
        if size < 32:
            raise _invalid()
        with document.openstream("WordDocument") as word:
            fib = word.read(32)  # olefile loads the stream; size was checked before this call.
        if struct.unpack_from("<H", fib)[0] != 0xA5EC:
            raise _invalid()
        flags = struct.unpack_from("<H", fib, 10)[0]
        if flags & 0x0100:
            raise _rejected()
        table = "1Table" if flags & 0x0200 else "0Table"
        if not document.exists(table) or document.get_size(table) == 0:
            raise _invalid()
        return DOC


def detect_file_type(source: BinaryIO, policy: UploadPolicy) -> str:
    """Identify actual format; this result never asserts that a file is CLEAN."""
    source.seek(0)
    header = source.read(8)
    source.seek(0)
    try:
        if header.startswith(b"%PDF-"):
            reader = PdfReader(source, strict=True)
            if reader.is_encrypted:
                raise _rejected()
            if "/Root" not in reader.trailer:
                raise _invalid()
            return PDF
        if header.startswith(b"PK\x03\x04"):
            return _probe_ooxml(source, policy)
        if header == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
            return _probe_doc(source, policy)
    except (
        PdfReadError,
        BadZipFile,
        ParseError,
        DefusedXmlException,
        OleFileError,
        EOFError,
        ValueError,
        KeyError,
        struct.error,
    ) as exc:
        raise _invalid() from exc
    raise UploadFailure(ErrorCode.UNSUPPORTED_FILE_TYPE, "无法识别为 PDF、DOCX、XLSX 或 DOC。")


_R_NAMESPACES = {
    "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "http://purl.oclc.org/ooxml/officeDocument/relationships",
}
_ZIP_HEADERS = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_OLE_HEADER = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _consume(totals: dict[str, int], size: int, policy: UploadPolicy) -> None:
    totals["expanded_bytes"] += size
    if totals["expanded_bytes"] > policy.max_container_expanded_bytes:
        raise _limit()


def _parse_xml(source: BinaryIO, limit: int) -> Element:
    parser = XMLParser(
        target=_BoundedTreeBuilder(), forbid_dtd=True, forbid_entities=True, forbid_external=True
    )
    source.seek(0)
    size = 0
    while block := source.read(min(65536, limit + 1 - size)):
        size += len(block)
        if size > limit:
            raise _limit()
        parser.feed(block)
    return parser.close()


def _relationship_owner(name: str) -> str:
    if name == "_rels/.rels":
        return ""
    parent, filename = posixpath.split(name)
    if posixpath.basename(parent) != "_rels" or not filename.endswith(".rels"):
        raise _invalid()
    return posixpath.join(posixpath.dirname(parent), filename[:-5])


def _target_part(owner: str, target: str) -> str:
    uri = urlsplit(target)
    path = unquote(uri.path)
    if uri.scheme or uri.netloc or uri.query or not path or "\\" in path:
        raise _invalid()
    if path.startswith("/"):
        resolved = posixpath.normpath(path[1:])
    else:
        resolved = posixpath.normpath(posixpath.join(posixpath.dirname(owner), path))
    # Relative '..' is valid inside a package, but cannot escape its root.
    return _part_name(resolved)


def _is_macro(content_type: str) -> bool:
    return any(
        marker in content_type.lower() for marker in ("macroenabled", "vbaproject", "macrosheet")
    )


def _check_package(
    parts: set[str],
    types: Element,
    relations: dict[str, Element],
    refs: dict[str, set[str]],
    tags: dict[str, str],
    workbooks: dict[str, Element],
) -> None:
    if types.tag != f"{{{_CT}}}Types" or "_rels/.rels" not in relations:
        raise _invalid()
    overrides: dict[str, str] = {}
    defaults: dict[str, str] = {}
    for node in types:
        content_type = node.get("ContentType", "")
        if _is_macro(content_type):
            raise _rejected()
        if node.tag == f"{{{_CT}}}Override":
            name = node.get("PartName", "")
            if not name.startswith("/"):
                raise _invalid()
            name = _part_name(name[1:])
            if name in overrides or name not in parts:
                raise _invalid()
            overrides[name] = content_type
        elif node.tag == f"{{{_CT}}}Default":
            extension = node.get("Extension", "").lower()
            if not extension or extension in defaults:
                raise _invalid()
            defaults[extension] = content_type
        else:
            raise _invalid()
    for name in parts - {"[Content_Types].xml"}:
        if not overrides.get(name, defaults.get(name.rsplit(".", 1)[-1].lower())):
            raise _invalid()
    by_owner: dict[str, dict[str, tuple[str, str | None]]] = {}
    main_parts: list[str] = []
    for name, root in relations.items():
        if root.tag != f"{{{_REL}}}Relationships":
            raise _invalid()
        owner = _relationship_owner(name)
        if owner and owner not in parts:
            raise _invalid()
        entries: dict[str, tuple[str, str | None]] = {}
        for node in root:
            identifier = node.get("Id", "")
            kind = node.get("Type", "")
            target = node.get("Target", "")
            if (
                node.tag != f"{{{_REL}}}Relationship"
                or not identifier
                or not kind
                or not target
                or identifier in entries
            ):
                raise _invalid()
            mode = node.get("TargetMode", "Internal")
            if mode == "External":
                if kind in _OFFICE_RELS:
                    raise _invalid()
                entries[identifier] = kind, None  # Never fetch external relationships.
            elif mode == "Internal":
                resolved = _target_part(owner, target)
                if resolved not in parts:
                    raise _invalid()
                entries[identifier] = kind, resolved
                if owner == "" and kind in _OFFICE_RELS:
                    main_parts.append(resolved)
            else:
                raise _invalid()
        by_owner[owner] = entries
    if len(main_parts) != 1:
        raise _invalid()
    main = main_parts[0]
    media = _MAIN_TYPES.get(overrides.get(main, ""))
    if media is None or tags.get(main) not in _MAIN_ROOTS[media]:
        raise _invalid()
    for owner, identifiers in refs.items():
        if not identifiers.issubset(by_owner.get(owner, {})):
            raise _invalid()
    if media == XLSX:
        workbook = workbooks.get(main)
        if workbook is None:
            raise _invalid()
        sheet_count = 0
        for node in workbook.iter():
            if node.tag.rsplit("}", 1)[-1] != "sheet":
                continue
            sheet_count += 1
            identifier = next(
                (
                    node.get(f"{{{namespace}}}id")
                    for namespace in _R_NAMESPACES
                    if node.get(f"{{{namespace}}}id") is not None
                ),
                None,
            )
            if not identifier or identifier not in by_owner.get(main, {}):
                raise _invalid()
            kind, target = by_owner[main][identifier]
            if target is None or kind.rsplit("/", 1)[-1] not in {
                "worksheet",
                "chartsheet",
                "dialogsheet",
            }:
                raise _invalid()
            expected = kind.rsplit("/", 1)[-1]
            if tags.get(target, "").rsplit("}", 1)[-1] != expected:
                raise _invalid()
        if sheet_count == 0:
            raise _invalid()


def _nested(source: BinaryIO, policy: UploadPolicy, totals: dict[str, int], depth: int) -> None:
    source.seek(0)
    header = source.read(8)
    source.seek(0)
    if header.startswith(_ZIP_HEADERS):
        _check_zip(source, policy, totals, depth + 1)
    elif header == _OLE_HEADER:
        _check_ole(source, policy, totals, depth + 1)
    elif header.startswith(b"%PDF-"):
        # Embedded encrypted PDF is subject to the same fixed reject policy.
        detect_file_type(source, policy)


def _check_zip(source: BinaryIO, policy: UploadPolicy, totals: dict[str, int], depth: int) -> None:
    if depth > policy.max_container_depth:
        raise _limit()
    _zip_preflight(source, policy)
    with ZipFile(source) as package:
        entries = package.infolist()
        totals["entries"] += len(entries)
        if totals["entries"] > policy.max_container_entries:
            raise _limit()
        parts: set[str] = set()
        relations: dict[str, Element] = {}
        refs: dict[str, set[str]] = {}
        tags: dict[str, str] = {}
        workbooks: dict[str, Element] = {}
        types: Element | None = None
        names: set[str] = set()
        is_package = "[Content_Types].xml" in package.namelist()
        for entry in entries:
            name = _part_name(entry.orig_filename)
            if name in names:
                raise _invalid()
            names.add(name)
            if entry.flag_bits & 1:
                raise _rejected()
            if "vbaproject" in name.lower() or name.lower().endswith("vba.bin"):
                raise _rejected()
            if entry.file_size > policy.max_container_member_bytes:
                raise _limit()
            if entry.is_dir():
                continue
            parts.add(name)
            # CPython ZipExtFile truncates output at ZipInfo.file_size. Read using
            # a private metadata copy whose bound is above our own stopping limit;
            # preserve the real compressed size/CRC, then compare actual size to
            # the original declaration. This prevents forged size+CRC prefixes
            # from concealing actual expansion. No original metadata is modified.
            read_info = copy(entry)
            read_info.file_size = policy.max_container_member_bytes + 65537
            with (
                package.open(read_info) as member,
                SpooledTemporaryFile(max_size=1024**2) as staged,
            ):
                member_bytes = 0
                while block := member.read(
                    min(65536, policy.max_container_member_bytes + 1 - member_bytes)
                ):
                    member_bytes += len(block)
                    if member_bytes > policy.max_container_member_bytes:
                        raise _limit()
                    _consume(totals, len(block), policy)
                    staged.write(block)
                if member_bytes != entry.file_size:
                    raise _invalid()
                _nested(cast(BinaryIO, staged), policy, totals, depth)
                if is_package and (name.endswith(".xml") or name.endswith(".rels")):
                    limit = (
                        policy.max_container_directory_bytes
                        if name.endswith(".rels") or name == "[Content_Types].xml"
                        else policy.max_container_member_bytes
                    )
                    root = _parse_xml(cast(BinaryIO, staged), limit)
                    tags[name] = root.tag
                    identifiers: set[str] = set()
                    for node in root.iter():
                        for namespace in _R_NAMESPACES:
                            for attr in ("id", "embed", "link"):
                                identifier = node.get(f"{{{namespace}}}{attr}")
                                if identifier is not None:
                                    identifiers.add(identifier)
                    if identifiers:
                        refs[name] = identifiers
                    if name == "[Content_Types].xml":
                        types = root
                    elif name.endswith(".rels"):
                        relations[name] = root
                    elif root.tag in _MAIN_ROOTS[XLSX]:
                        workbooks[name] = root
        if is_package:
            if types is None:
                raise _invalid()
            _check_package(parts, types, relations, refs, tags, workbooks)


def _check_ole(source: BinaryIO, policy: UploadPolicy, totals: dict[str, int], depth: int) -> None:
    if depth > policy.max_container_depth:
        raise _limit()
    _ole_preflight(source, policy)
    with olefile.OleFileIO(source, raise_defects=olefile.DEFECT_INCORRECT) as document:
        names = document.listdir(streams=True, storages=True)
        totals["entries"] += len(names)
        if totals["entries"] > policy.max_container_entries:
            raise _limit()
        for name in names:
            if any(
                part.lower()
                in {"encryptioninfo", "encryptedpackage", "macros", "vba", "_vba_project"}
                for part in name
            ):
                raise _rejected()
        for name in document.listdir(streams=True, storages=False):
            size = document.get_size(name)
            if size > policy.max_container_member_bytes:
                raise _limit()
            # olefile eagerly loads a stream; bounds are checked before openstream.
            _consume(totals, size, policy)
            with (
                document.openstream(name) as member,
                SpooledTemporaryFile(max_size=1024**2) as staged,
            ):
                actual_size = 0
                while block := member.read(65536):
                    actual_size += len(block)
                    if actual_size > size:
                        raise _invalid()
                    staged.write(block)
                if actual_size != size:
                    raise _invalid()
                if name == ["WordDocument"]:
                    _check_fib(cast(BinaryIO, staged))
                _nested(cast(BinaryIO, staged), policy, totals, depth)


def _check_fib(source: BinaryIO) -> None:
    # MS-DOC 2.5.1 / 2.5.14: section counts use the effective nFib, which
    # comes from nFibNew when cswNew is nonzero, not necessarily FibBase.nFib.
    source.seek(0)
    header = source.read(154)
    if len(header) != 154 or struct.unpack_from("<H", header)[0] != 0xA5EC:
        raise _invalid()
    if (
        struct.unpack_from("<H", header, 32)[0] != 14
        or struct.unpack_from("<H", header, 62)[0] != 22
    ):
        raise _invalid()
    blob_count = struct.unpack_from("<H", header, 152)[0]
    expected = {0xC1: (93, 0), 0xD9: (108, 2), 0x101: (136, 2), 0x10C: (164, 2), 0x112: (183, 5)}
    if blob_count not in {value[0] for value in expected.values()}:
        raise _invalid()
    source.seek(154 + 8 * blob_count)
    count_data = source.read(2)
    if len(count_data) != 2:
        raise _invalid()
    new_count = struct.unpack("<H", count_data)[0]
    if new_count not in {0, 2, 5}:
        raise _invalid()
    new_data = source.read(new_count * 2)
    if len(new_data) != new_count * 2:
        raise _invalid()
    version = (
        struct.unpack_from("<H", new_data)[0]
        if new_count
        else struct.unpack_from("<H", header, 2)[0]
    )
    if expected.get(version) != (blob_count, new_count):
        raise _invalid()


def check_document(source: BinaryIO, policy: UploadPolicy) -> str:
    """Validate the permitted document and contained ZIP/OLE/PDF inputs."""
    try:
        media = detect_file_type(source, policy)
        totals = {"entries": 0, "expanded_bytes": 0}
        source.seek(0)
        if media in {DOCX, XLSX}:
            _check_zip(source, policy, totals, 1)
        elif media == DOC:
            _check_ole(source, policy, totals, 1)
        elif media == PDF:
            reader = PdfReader(source, strict=True)
            root = reader.root_object
            if root.get("/Type") != "/Catalog" or "/Pages" not in root:
                raise _invalid()
        return media
    except UploadFailure as exc:
        raise UploadFailure(
            exc.error.code,
            exc.error.user_message,
            retryable=exc.error.retryable,
            stage="D02",
            detail_ref=exc.error.detail_ref,
        ) from exc
    except (
        BadZipFile,
        zlib.error,
        ParseError,
        DefusedXmlException,
        OleFileError,
        PdfReadError,
        ValueError,
        KeyError,
        EOFError,
    ) as exc:
        failure = _invalid()
        raise UploadFailure(failure.error.code, failure.error.user_message, stage="D02") from exc
