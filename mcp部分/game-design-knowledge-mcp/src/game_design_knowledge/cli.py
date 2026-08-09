from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
from typing import Sequence
import uuid

from .indexer import index_documents


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="game-design-knowledge")
    commands = parser.add_subparsers(dest="command", required=True)
    index_parser = commands.add_parser("index", help="Build an index from design documents")
    index_parser.add_argument("source", type=Path)
    index_parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    report = _build_index_atomically(arguments.source, arguments.output)
    print(json.dumps(report, ensure_ascii=False))
    return 0


def _build_index_atomically(source: Path, output: Path) -> dict[str, int]:
    output = output.resolve()
    if output == Path(output.anchor):
        raise ValueError("Index output must not be a filesystem root")
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = output.parent / f".{output.name}.staging-{uuid.uuid4().hex}"
    backup = output.parent / f".{output.name}.backup-{uuid.uuid4().hex}"

    try:
        if output.exists():
            if not output.is_dir():
                raise ValueError(f"Index output exists and is not a directory: {output}")
            shutil.copytree(output, staging)
        report = index_documents(source, staging)
        if output.exists():
            output.rename(backup)
        try:
            staging.rename(output)
        except BaseException:
            if backup.exists() and not output.exists():
                backup.rename(output)
            raise
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        return report
    except BaseException:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
