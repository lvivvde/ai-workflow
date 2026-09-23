"""Small DOCX/XLSX/image fixtures shared by the index, snapshot, and OCR tests."""

from __future__ import annotations

from pathlib import Path
import struct
import zlib
import zipfile


def write_docx(path: Path, text: str) -> None:
    """One-paragraph DOCX whose bytes change when the text changes."""

    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>
</w:document>
"""
    relationships_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/_rels/document.xml.rels", relationships_xml)


def write_docx_with_image(
    path: Path,
    text: str,
    image_bytes: bytes,
    *,
    media_name: str = "image1.png",
) -> None:
    """One-paragraph DOCX that embeds ``image_bytes`` next to the text.

    The image's relationship id is what the indexer resolves back to a media
    part, so the fixture has to write both the drawing and the relationship.
    """

    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
  xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
  xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <w:body>
    <w:p>
      <w:r><w:t>{text}</w:t></w:r>
      <w:r><w:drawing><a:blip r:embed="rId5"/></w:drawing></w:r>
    </w:p>
  </w:body>
</w:document>
"""
    relationships_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/{media_name}"/>
</Relationships>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/_rels/document.xml.rels", relationships_xml)
        archive.writestr(f"word/media/{media_name}", image_bytes)


def write_xlsx_with_image(
    path: Path,
    text: str,
    image_bytes: bytes,
    *,
    sheet_name: str = "数值配置",
    media_name: str = "mockup.png",
) -> None:
    """One-sheet XLSX whose ``C5`` cell sits next to an anchored picture."""

    workbook_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheets><sheet name="{sheet_name}" sheetId="1" r:id="rId1"/></sheets>
</workbook>
"""
    workbook_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
</Relationships>
"""
    sheet_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <sheetData><row r="5"><c r="C5" t="inlineStr"><is><t>{text}</t></is></c></row></sheetData>
  <drawing r:id="rId2"/>
</worksheet>
"""
    sheet_rels = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" Target="../drawings/drawing1.xml"/>
</Relationships>
"""
    drawing_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <xdr:oneCellAnchor>
    <xdr:from><xdr:col>2</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>4</xdr:row><xdr:rowOff>0</xdr:rowOff></xdr:from>
    <xdr:pic><xdr:blipFill><a:blip r:embed="rId3"/></xdr:blipFill></xdr:pic>
    <xdr:clientData/>
  </xdr:oneCellAnchor>
</xdr:wsDr>
"""
    drawing_rels = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/{media_name}"/>
</Relationships>
"""
    content_types = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
</Types>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("xl/workbook.xml", workbook_xml)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml)
        archive.writestr("xl/worksheets/_rels/sheet1.xml.rels", sheet_rels)
        archive.writestr("xl/drawings/drawing1.xml", drawing_xml)
        archive.writestr("xl/drawings/_rels/drawing1.xml.rels", drawing_rels)
        archive.writestr(f"xl/media/{media_name}", image_bytes)


def png_bytes(width: int = 8, height: int = 8, colour: tuple[int, int, int] = (10, 20, 30)) -> bytes:
    """A real, decodable PNG. No image library is needed to build one."""

    return _png(width, height, [bytes(colour) * width] * height)


def write_png(path: Path, **kwargs: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png_bytes(**kwargs))  # type: ignore[arg-type]


def arrow_png(
    direction: str,
    *,
    box: tuple[int, int, int, int] = (60, 40, 40, 60),
    size: tuple[int, int] = (200, 160),
) -> bytes:
    """A white PNG with a black arrow drawn inside ``box``.

    The shape is the one the arrow rules and the ink measurement are meant to
    read: a stem with a wider head at the end the arrow points to, so the widest
    band of ink sits at the head -- near the far end for ``down``/``right`` and
    near the near end for ``up``/``left``.

    Spelled out row by row rather than drawn with an image library, so a test
    can hand the pipeline real pixels for an arrow block without depending on
    Pillow to make them (issue #28).
    """

    if direction not in ("up", "down", "left", "right"):
        raise ValueError(f"not an arrow direction: {direction!r}")
    width, height = size
    left, top, box_width, box_height = box
    white = (255, 255, 255)
    rows = [bytearray(bytes(white) * width) for _ in range(height)]
    head = 0.3

    def ink(x: int, y: int) -> None:
        if 0 <= x < width and 0 <= y < height:
            rows[y][x * 3 : x * 3 + 3] = bytes((0, 0, 0))

    if direction in ("up", "down"):
        span = max(1, round(box_height * head))
        stem_left = left + box_width // 3
        stem_right = left + box_width - box_width // 3
        for y in range(top, top + box_height):
            if direction == "up":
                depth = y - top
                half = round((box_width / 2) * (depth / span)) if depth < span else None
            else:
                depth = top + box_height - 1 - y
                half = round((box_width / 2) * (depth / span)) if depth < span else None
            if half is None:
                start, end = stem_left, stem_right
            else:
                centre = left + box_width // 2
                start, end = centre - half, centre + half
            for x in range(start, end + 1):
                ink(x, y)
    else:
        span = max(1, round(box_width * head))
        stem_top = top + box_height // 3
        stem_bottom = top + box_height - box_height // 3
        for x in range(left, left + box_width):
            if direction == "right":
                depth = left + box_width - 1 - x
                half = round((box_height / 2) * (depth / span)) if depth < span else None
            else:
                depth = x - left
                half = round((box_height / 2) * (depth / span)) if depth < span else None
            if half is None:
                start, end = stem_top, stem_bottom
            else:
                centre = top + box_height // 2
                start, end = centre - half, centre + half
            for y in range(start, end + 1):
                ink(x, y)
    return _png(width, height, [bytes(row) for row in rows])


def _png(width: int, height: int, rows: list[bytes]) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + row for row in rows)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def jpeg_bytes() -> bytes:
    """A minimal JPEG: signature, one component, and an end marker.

    It is not a photograph, but it *is* a file whose signature says JPEG, which
    is what the preflight checks and what a fake OCR provider needs.
    """

    return (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xdb\x00\x43\x00"
        + bytes(range(1, 65))
        + b"\xff\xc0\x00\x0b\x08\x00\x08\x00\x08\x01\x01\x11\x00"
        b"\xff\xc4\x00\x14\x00\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
        b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\xd2\xcf\x20\xff\xd9"
    )


def write_jpeg(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(jpeg_bytes())


__all__ = [
    "arrow_png",
    "jpeg_bytes",
    "png_bytes",
    "write_docx",
    "write_docx_with_image",
    "write_jpeg",
    "write_png",
    "write_xlsx_with_image",
]
