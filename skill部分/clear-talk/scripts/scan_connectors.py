#!/usr/bin/env python3
"""Report contrast and transition markers that deserve a manual review."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import sys
from collections.abc import Iterable


TEXT_SUFFIXES = {".md", ".mdx", ".txt", ".rst", ".adoc"}
PATTERNS = (
    ("否定转折", re.compile(r"(?:并不|不是|并非|不只是).{0,80}?(?:而是|而在于)")),
    ("反向对比", re.compile(r"(?:而不是|而非).{1,80}")),
    ("取舍结构", re.compile(r"与其.{1,80}?不如")),
    ("让步转折", re.compile(r"(?:虽然|尽管|即使).{0,80}?(?:但是|但|却|仍然|也)")),
    ("递进结构", re.compile(r"(?:不仅|不只是).{0,80}?(?:还|而且|也)")),
    ("预设纠正", re.compile(r"(?:这并不意味着|真正的问题是|更准确地说|看似.{0,40}?(?:实际上|其实))")),
    ("句首转折", re.compile(r"^\s*(?:但|但是|然而|不过|其实|反而|仍然|即便如此|进一步说)[，,:：\s]")),
    ("not-but", re.compile(r"\bnot\b.{0,100}?\bbut\b", re.IGNORECASE)),
    ("rather-than", re.compile(r"\brather\s+than\b", re.IGNORECASE)),
    ("leading-transition", re.compile(r"^\s*(?:however|actually|instead|rather|nevertheless|nonetheless)\b", re.IGNORECASE)),
)


def iter_files(inputs: Iterable[str]) -> Iterable[Path]:
    for raw in inputs:
        path = Path(raw)
        if path.is_file():
            yield path
        elif path.is_dir():
            yield from (
                candidate
                for candidate in path.rglob("*")
                if candidate.is_file() and candidate.suffix.lower() in TEXT_SUFFIXES
            )
        else:
            print(f"scan_connectors: path not found: {path}", file=sys.stderr)


def scan(path: Path) -> int:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        print(f"scan_connectors: cannot read {path}: {exc}", file=sys.stderr)
        return 0

    findings = 0
    fence = None
    for number, line in enumerate(lines, 1):
        stripped = line.lstrip()
        marker = stripped[:3]
        if marker in {"```", "~~~"}:
            fence = None if fence == marker else marker
            continue
        if fence:
            continue
        for label, pattern in PATTERNS:
            if pattern.search(line):
                print(f"{path}:{number}: {label}: {line.strip()}")
                findings += 1
                break
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report possible unearned contrasts and transitions without modifying files."
    )
    parser.add_argument("paths", nargs="+", help="Text files or directories to scan")
    args = parser.parse_args()

    files = list(iter_files(args.paths))
    if not files:
        return 2

    findings = sum(scan(path) for path in files)
    print(f"scan_connectors: {findings} review candidate(s) in {len(files)} file(s)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
