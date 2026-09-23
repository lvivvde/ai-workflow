"""Materialize synthetic corpus documents as real OOXML files.

Corpora ship generator specs rather than binary documents, so the public repo
holds only synthetic or desensitized material and every sample stays diffable.
An image document points at a committed PNG under the corpus root (``asset``)
instead of inlining bytes, so the picture a sample annotates can be opened and
reviewed next to the labels - the pixel content is synthetic either way.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping
import zipfile

from .schema import DocumentSpec, Sample, SchemaError


# A 1x1 opaque PNG; large enough to be a real image, small enough to inline.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
    "IQAAAABJRU5ErkJggg=="
)

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

CONTENT_TYPES_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
</Types>
"""

EMPTY_RELATIONSHIPS_XML = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>
"""


def materialize_corpus_samples(
    samples: Iterable[Sample], destination: Path
) -> dict[str, list[Path]]:
    """Write every sample document once, failing on conflicting duplicates."""

    destination.mkdir(parents=True, exist_ok=True)
    written: dict[str, list[Path]] = {}
    seen: dict[Path, str] = {}
    for sample in samples:
        paths: list[Path] = []
        for document in sample.documents:
            target = (destination / document.path).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise SchemaError(
                    f"{sample.sample_id}: document escapes the corpus workspace: "
                    f"{document.path}"
                )
            fingerprint = _generator_fingerprint(document.generator)
            previous = seen.get(target)
            if previous is not None and previous != fingerprint:
                raise SchemaError(
                    f"Two samples describe different content for the same path: "
                    f"{document.path}"
                )
            if previous is None:
                seen[target] = fingerprint
                materialize_document(document, destination)
            paths.append(target)
        written[sample.sample_id] = paths
    return written


def materialize_document(document: DocumentSpec, destination: Path) -> Path:
    target = (destination / document.path).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    kind = document.generator.get("kind")
    if kind == "docx":
        _write_docx(target, document.generator, document.asset_root)
    elif kind == "xlsx":
        _write_xlsx(target, document.generator)
    elif kind == "catalog":
        _write_catalog(target, document.generator)
    elif kind == "png":
        _write_png(target, document.generator, document.asset_root)
    else:  # pragma: no cover - guarded by the schema
        raise SchemaError(f"Unsupported generator kind: {kind!r}")
    return target


def _generator_fingerprint(generator: Mapping[str, Any]) -> str:
    from .schema import canonical_fingerprint

    return canonical_fingerprint(generator)


def _asset_payload(
    asset: Any, asset_sha256: Any, root: Path | None, where: Path
) -> bytes:
    """Read one committed corpus asset and check it is what the spec pinned."""

    if not isinstance(asset, str) or not asset.strip():
        raise SchemaError(f"{where}: asset must be a non-empty relative path")
    if not _is_sha256(asset_sha256):
        raise SchemaError(
            f"{where}: asset {asset!r} needs the sha256 of the committed picture; "
            "an unpinned asset would let the annotation drift away from the pixels"
        )
    relative = Path(asset)
    if relative.is_absolute() or ".." in relative.parts:
        raise SchemaError(f"{where}: asset must stay inside the corpus: {asset}")
    if root is None:
        raise SchemaError(
            f"{where}: asset {asset!r} needs the corpus directory it belongs to; "
            "load the corpus with load_corpus() before materializing it"
        )
    corpus_root = Path(root).resolve()
    source = (corpus_root / relative).resolve()
    if not source.is_relative_to(corpus_root):
        raise SchemaError(f"{where}: asset escapes the corpus: {asset}")
    if not source.is_file():
        raise SchemaError(f"{where}: corpus asset is missing: {asset}")
    payload = source.read_bytes()
    if not payload.startswith(PNG_SIGNATURE):
        raise SchemaError(f"{where}: corpus asset must be a PNG: {asset}")
    expected = str(asset_sha256).lower()
    actual = hashlib.sha256(payload).hexdigest()
    if actual != expected:
        raise SchemaError(
            f"{where}: asset {asset} does not match the pinned sha256; "
            f"the picture and its annotation drifted apart (expected "
            f"{expected}, got {actual})"
        )
    return payload


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(character in "0123456789abcdefABCDEF" for character in value)


def _image_payload(block: Mapping[str, Any], where: Path, root: Path | None) -> bytes:
    """The bytes for one image document or embedded-image block."""

    if block.get("asset") is None:
        if block.get("asset_sha256") is not None:
            raise SchemaError(
                f"{where}: asset_sha256 without asset would be ignored, and the "
                "document would quietly fall back to the placeholder picture"
            )
        return TINY_PNG
    return _asset_payload(block.get("asset"), block.get("asset_sha256"), root, where)


def _write_docx(
    path: Path, generator: Mapping[str, Any], asset_root: Path | None = None
) -> None:
    blocks = generator.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise SchemaError(f"docx generator needs a non-empty blocks list: {path}")
    media: list[tuple[str, bytes]] = []
    relationships: list[tuple[str, str, str]] = []
    body = "".join(
        _docx_block(block, path, media, relationships, asset_root) for block in blocks
    )
    document_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '\n<w:document xmlns:w="http://schemas.openxmlformats.org/'
        'wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/'
        'officeDocument/2006/relationships" xmlns:a="http://schemas.openxmlformats.'
        'org/drawingml/2006/main">'
        f"\n  <w:body>{body}</w:body>\n</w:document>\n"
    )
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES_XML)
        archive.writestr("word/document.xml", document_xml)
        archive.writestr(
            "word/_rels/document.xml.rels", _relationships_xml(relationships)
        )
        for part, payload in media:
            archive.writestr(part, payload)


def _docx_block(
    block: Mapping[str, Any],
    path: Path,
    media: list[tuple[str, bytes]],
    relationships: list[tuple[str, str, str]],
    asset_root: Path | None = None,
) -> str:
    if not isinstance(block, Mapping):
        raise SchemaError(f"docx blocks must be objects: {path}")
    block_type = block.get("type")
    if block_type == "image":
        relationship_id = str(block.get("relationship_id") or "rIdImage1")
        media_name = str(block.get("media_name") or "word/media/image1.png")
        relationships.append(
            (
                relationship_id,
                "http://schemas.openxmlformats.org/officeDocument/2006/"
                "relationships/image",
                media_name.replace("word/", "", 1),
            )
        )
        media.append((media_name, _image_payload(block, path, asset_root)))
        return (
            "<w:p><w:r><w:drawing>"
            '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/'
            'drawingml/2006/picture">'
            f'<a:blip r:embed="{relationship_id}"/>'
            "</a:graphicData></a:graphic>"
            "</w:drawing></w:r></w:p>"
        )
    if block_type == "heading":
        level = int(block.get("level", 1))
        return (
            f'<w:p><w:pPr><w:pStyle w:val="Heading{level}"/></w:pPr>'
            f"<w:r><w:t>{_escape(block.get('text'))}</w:t></w:r></w:p>"
        )
    if block_type == "paragraph":
        return f"<w:p><w:r><w:t>{_escape(block.get('text'))}</w:t></w:r></w:p>"
    if block_type == "list_item":
        level = int(block.get("level", 0))
        return (
            "<w:p><w:pPr><w:numPr>"
            f'<w:ilvl w:val="{level}"/><w:numId w:val="1"/>'
            "</w:numPr></w:pPr>"
            f"<w:r><w:t>{_escape(block.get('text'))}</w:t></w:r></w:p>"
        )
    if block_type == "table":
        rows = block.get("rows") or []
        if not isinstance(rows, list):
            raise SchemaError(f"docx table rows must be a list: {path}")
        rendered_rows = "".join(
            "<w:tr>"
            + "".join(
                f"<w:tc><w:p><w:r><w:t>{_escape(cell)}</w:t></w:r></w:p></w:tc>"
                for cell in row
            )
            + "</w:tr>"
            for row in rows
        )
        return f"<w:tbl>{rendered_rows}</w:tbl>"
    raise SchemaError(f"Unsupported docx block type {block_type!r}: {path}")


def _relationships_xml(relationships: list[tuple[str, str, str]]) -> str:
    if not relationships:
        return EMPTY_RELATIONSHIPS_XML
    entries = "".join(
        f'<Relationship Id="{relationship_id}" Type="{relationship_type}" '
        f'Target="{target}"/>'
        for relationship_id, relationship_type, target in relationships
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
        f'relationships">{entries}</Relationships>\n'
    )


def _write_catalog(path: Path, generator: Mapping[str, Any]) -> None:
    features = generator.get("features")
    if not isinstance(features, list) or not features:
        raise SchemaError(f"catalog generator needs a non-empty features list: {path}")
    path.write_text(
        json.dumps({"features": features}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _write_png(
    path: Path, generator: Mapping[str, Any], asset_root: Path | None = None
) -> None:
    """A standalone image document, so the corpus covers loose image imports.

    With an ``asset`` the corpus ships a committed synthetic PNG whose pixels
    really carry the annotated text, so OCR, layout, and the arrow rules have
    something to observe. Without one the document stays a 1x1 placeholder, and
    the annotation says what the image is meant to say.
    """

    suffix = path.suffix.lower()
    if suffix not in {".png", ".jpg", ".jpeg"}:
        raise SchemaError(f"png generator needs a .png/.jpg/.jpeg path: {path}")
    path.write_bytes(_image_payload(generator, path, asset_root))


def _write_xlsx(path: Path, generator: Mapping[str, Any]) -> None:
    sheets = generator.get("sheets")
    if not isinstance(sheets, list) or not sheets:
        raise SchemaError(f"xlsx generator needs a non-empty sheets list: {path}")

    shared_strings: list[str] = []
    shared_index: dict[str, int] = {}
    sheet_xml_parts: list[str] = []
    sheet_entries: list[str] = []
    relationships: list[str] = []
    for position, sheet in enumerate(sheets, start=1):
        if not isinstance(sheet, Mapping):
            raise SchemaError(f"xlsx sheets must be objects: {path}")
        name = str(sheet.get("name") or f"Sheet{position}")
        rows = sheet.get("rows") or []
        if not isinstance(rows, list):
            raise SchemaError(f"xlsx sheet rows must be a list: {path}")
        cells_xml: list[str] = []
        max_column = 0
        for row_index, row in enumerate(rows, start=1):
            if not isinstance(row, list):
                raise SchemaError(f"xlsx rows must be lists: {path}")
            max_column = max(max_column, len(row))
            for column_index, value in enumerate(row, start=1):
                cell = _xlsx_cell(
                    row_index, column_index, value, shared_strings, shared_index
                )
                if cell:
                    cells_xml.append(cell)
        dimension = (
            f'<dimension ref="A1:{_column_name(max_column)}{max(len(rows), 1)}"/>'
            if rows
            else ""
        )
        sheet_xml_parts.append(
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '\n<worksheet xmlns="http://schemas.openxmlformats.org/'
            'spreadsheetml/2006/main">'
            f"{dimension}<sheetData>{''.join(cells_xml)}</sheetData></worksheet>\n"
        )
        sheet_entries.append(
            f'<sheet name="{_escape(name)}" sheetId="{position}" r:id="rId{position}"/>'
        )
        relationships.append(
            f'<Relationship Id="rId{position}" Type="http://schemas.openxmlformats.org/'
            f'officeDocument/2006/relationships/worksheet" '
            f'Target="worksheets/sheet{position}.xml"/>'
        )

    workbook_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '\n<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{''.join(sheet_entries)}</sheets></workbook>\n"
    )
    relationships_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '\n<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/'
        f'relationships">{"".join(relationships)}</Relationships>\n'
    )

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES_XML)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", relationships_xml)
        for position, sheet_xml in enumerate(sheet_xml_parts, start=1):
            archive.writestr(f"xl/worksheets/sheet{position}.xml", sheet_xml)
        if shared_strings:
            archive.writestr("xl/sharedStrings.xml", _shared_strings_xml(shared_strings))


def _xlsx_cell(
    row_index: int,
    column_index: int,
    value: Any,
    shared_strings: list[str],
    shared_index: dict[str, int],
) -> str:
    if value is None:
        return ""
    reference = f"{_column_name(column_index)}{row_index}"
    if isinstance(value, bool):
        return f'<c r="{reference}" t="b"><v>{1 if value else 0}</v></c>'
    if isinstance(value, (int, float)):
        return f'<c r="{reference}"><v>{value}</v></c>'
    text = str(value)
    index = shared_index.get(text)
    if index is None:
        index = len(shared_strings)
        shared_index[text] = index
        shared_strings.append(text)
    return f'<c r="{reference}" t="s"><v>{index}</v></c>'


def _shared_strings_xml(values: list[str]) -> str:
    items = "".join(f"<si><t>{_escape(value)}</t></si>" for value in values)
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '\n<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        f'count="{len(values)}" uniqueCount="{len(values)}">{items}</sst>\n'
    )


def _column_name(one_based_column: int) -> str:
    column = max(one_based_column, 1)
    letters = ""
    while column:
        column, remainder = divmod(column - 1, 26)
        letters = chr(65 + remainder) + letters
    return letters


def _escape(value: Any) -> str:
    text = "" if value is None else str(value)
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
