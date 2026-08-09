from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
from time import perf_counter
import zipfile

from game_design_knowledge.cli import _build_index_atomically
from game_design_knowledge.server import search_evidence


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate a synthetic DOCX corpus and benchmark build/reuse/query times."
    )
    parser.add_argument("--documents", type=int, default=1000)
    parser.add_argument("--max-first-seconds", type=float)
    parser.add_argument("--max-reuse-seconds", type=float)
    parser.add_argument("--max-query-seconds", type=float)
    arguments = parser.parse_args()
    if arguments.documents < 1:
        parser.error("--documents must be positive")

    with tempfile.TemporaryDirectory() as temporary_directory:
        workspace = Path(temporary_directory)
        source = workspace / "source"
        output = workspace / "index"
        source.mkdir()
        for index in range(arguments.documents):
            _write_docx(source / f"玩法-{index:05d}.docx", index)

        started = perf_counter()
        first_report = _build_index_atomically(source, output)
        first_seconds = perf_counter() - started

        started = perf_counter()
        reuse_report = _build_index_atomically(source, output)
        reuse_seconds = perf_counter() - started

        previous_index = os.environ.get("GAME_DESIGN_INDEX_DIR")
        os.environ["GAME_DESIGN_INDEX_DIR"] = os.fspath(output)
        try:
            started = perf_counter()
            query = search_evidence(f"玩法{arguments.documents - 1}", limit=5)
            query_seconds = perf_counter() - started
        finally:
            if previous_index is None:
                os.environ.pop("GAME_DESIGN_INDEX_DIR", None)
            else:
                os.environ["GAME_DESIGN_INDEX_DIR"] = previous_index

        result = {
            "documents": arguments.documents,
            "first_build_seconds": round(first_seconds, 3),
            "reuse_build_seconds": round(reuse_seconds, 3),
            "query_seconds": round(query_seconds, 3),
            "first_report": first_report,
            "reuse_report": reuse_report,
            "query_status": query["status"],
        }
        print(json.dumps(result, ensure_ascii=False, indent=2))

    limits = (
        ("first build", first_seconds, arguments.max_first_seconds),
        ("reuse build", reuse_seconds, arguments.max_reuse_seconds),
        ("query", query_seconds, arguments.max_query_seconds),
    )
    failures = [
        f"{name} took {actual:.3f}s, over {maximum:.3f}s"
        for name, actual, maximum in limits
        if maximum is not None and actual > maximum
    ]
    if query["status"] != "found":
        failures.append("benchmark query did not find its generated document")
    if reuse_report["documents_reused"] != arguments.documents:
        failures.append("incremental build did not reuse every unchanged document")
    if failures:
        raise SystemExit("; ".join(failures))
    return 0


def _write_docx(path: Path, index: int) -> None:
    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>玩法{index}</w:t></w:r></w:p>
    <w:p><w:r><w:t>玩法{index}每日开放5次。</w:t></w:r></w:p>
  </w:body>
</w:document>
"""
    relationships_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>
"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/_rels/document.xml.rels", relationships_xml)


if __name__ == "__main__":
    raise SystemExit(main())
