from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from .capabilities import CapabilityRuntime
from .index_build import build_index_atomically
from .migration import SchemaVersionError, apply_migration, plan_migration
from .pipeline import run_pipeline
from .processing import ProcessingError
from .recording import record_build_revisions


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="game-design-knowledge")
    commands = parser.add_subparsers(dest="command", required=True)
    index_parser = commands.add_parser("index", help="Build an index from design documents")
    index_parser.add_argument("source", type=Path)
    index_parser.add_argument("--output", type=Path, required=True)
    index_parser.add_argument(
        "--project-root",
        type=Path,
        default=None,
        help=(
            "Project that owns the documents. Inferred from an "
            "<root>/.index/<name> output directory when omitted; durable state is "
            "recorded only for documents inside it."
        ),
    )
    index_parser.add_argument(
        "--low-memory",
        action="store_true",
        help="Unload capability packs after each batch instead of keeping them warm.",
    )
    index_parser.add_argument(
        "--idle-timeout",
        type=float,
        default=None,
        help="Seconds a loaded capability pack may stay resident while idle.",
    )
    commands.add_parser(
        "capabilities",
        help="Report capability packs, hardware profile, and model residency",
    )
    migrate_parser = commands.add_parser(
        "migrate", help="Explicitly migrate an existing index to the current schema"
    )
    migrate_parser.add_argument("--database", type=Path, required=True)
    migrate_parser.add_argument("--backup-directory", type=Path)
    migrate_parser.add_argument("--plan", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    if arguments.command == "migrate":
        return _migrate(arguments)
    if arguments.command == "capabilities":
        print(json.dumps(CapabilityRuntime().status(), ensure_ascii=False, indent=2))
        return 0
    try:
        run = run_pipeline(
            arguments.source,
            arguments.output,
            low_memory=arguments.low_memory,
            idle_timeout=arguments.idle_timeout,
        )
        run.raise_for_blocking_failures()
    except ProcessingError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    report = dict(run.projection_report)
    report["processing"] = run.status()
    _record_durable_state(
        arguments.source, arguments.output, arguments.project_root
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0


def _record_durable_state(
    source: Path, output: Path, project_root: Path | None
) -> dict[str, object] | None:
    """Archive sources and record parse revisions after a successful publish."""

    output = output.resolve()
    root = _project_root_for(source, output, project_root)
    if root is None:
        return None
    if not source.resolve().is_relative_to(root):
        print(
            f"note: {source} is outside {root}; durable state records only "
            "documents inside the project root, so this build was not registered",
            file=sys.stderr,
        )
        return None
    summary = record_build_revisions(root, output)
    if summary["status"] != "recorded":
        print(
            f"warning: the index is published but durable state was not "
            f"recorded: {summary.get('error')}",
            file=sys.stderr,
        )
    return summary


def _project_root_for(
    source: Path, output: Path, project_root: Path | None
) -> Path | None:
    """The project that owns the documents, following the MCP server's rule."""

    if project_root is not None:
        return project_root.resolve()
    if output.parent.name == ".index":
        return output.parent.parent.resolve()
    return None


def _migrate(arguments: argparse.Namespace) -> int:
    plan = plan_migration(arguments.database)
    if arguments.plan:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 0 if plan["status"] in {"up_to_date", "migration_available"} else 1
    if plan["status"] not in {"up_to_date", "migration_available"}:
        raise SchemaVersionError(str(plan["message"]))
    report = apply_migration(
        arguments.database, backup_directory=arguments.backup_directory
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
