from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from .capabilities import CapabilityRuntime
from .index_build import build_index_atomically
from .migration import SchemaVersionError, apply_migration, plan_migration
from .offline import (
    BundleError,
    OfflineBundle,
    capability_doctor,
    uninstall_capability,
    write_capability_manifests,
)
from .pipeline import run_pipeline
from .processing import ProcessingError
from .recording import record_build_revisions
from .run_records import capability_baseline, write_run_record


CAPABILITY_MANIFEST_DIRECTORY = "capabilities/manifests"


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
    capability_parser = commands.add_parser(
        "capability",
        help="Install, verify, diagnose, or measure an offline capability pack",
    )
    capability_actions = capability_parser.add_subparsers(
        dest="action", required=True
    )
    status_parser = capability_actions.add_parser(
        "status", help="Report packs, hardware profile, and residency"
    )
    status_parser.add_argument("--profile")
    status_parser.add_argument("--model-root", type=Path)
    status_parser.add_argument("--low-memory", action="store_true")
    doctor_parser = capability_actions.add_parser(
        "doctor", help="Explain this machine's offline readiness"
    )
    doctor_parser.add_argument("--bundle", type=Path)
    doctor_parser.add_argument("--pack", action="append", dest="packs")
    doctor_parser.add_argument("--model-root", type=Path)
    plan_parser = capability_actions.add_parser(
        "plan", help="Preview an offline install from a bundle"
    )
    plan_parser.add_argument("--bundle", type=Path, required=True)
    plan_parser.add_argument("--pack", required=True)
    plan_parser.add_argument("--model-root", type=Path)
    install_parser = capability_actions.add_parser(
        "install", help="Install a pack from a pre-downloaded bundle"
    )
    install_parser.add_argument("--bundle", type=Path, required=True)
    install_parser.add_argument("--pack", required=True)
    install_parser.add_argument("--model-root", type=Path)
    install_parser.add_argument(
        "--confirm",
        action="store_true",
        help="Apply the install; without it only the plan is returned.",
    )
    install_parser.add_argument(
        "--apply-python",
        action="store_true",
        help="Also run the offline pip command instead of only reporting it.",
    )
    verify_parser = capability_actions.add_parser(
        "verify", help="Verify a bundle against this build's pins"
    )
    verify_parser.add_argument("--bundle", type=Path, required=True)
    verify_parser.add_argument("--pack", action="append", dest="packs")
    verify_parser.add_argument("--model-root", type=Path)
    uninstall_parser = capability_actions.add_parser(
        "uninstall", help="Remove a pack's model files"
    )
    uninstall_parser.add_argument("--pack", required=True)
    uninstall_parser.add_argument("--model-root", type=Path)
    uninstall_parser.add_argument("--confirm", action="store_true")
    manifests_parser = capability_actions.add_parser(
        "manifests", help="Print or refresh the committed capability manifests"
    )
    manifests_parser.add_argument("--write", type=Path)
    baseline_parser = capability_actions.add_parser(
        "baseline", help="Run one measured pass and write a run record"
    )
    baseline_parser.add_argument("--profile", default=None)
    baseline_parser.add_argument("--model-root", type=Path)
    baseline_parser.add_argument("--index-dir", type=Path)
    baseline_parser.add_argument("--query")
    baseline_parser.add_argument(
        "--output",
        type=Path,
        help="Append the record as one JSON line to this file.",
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
    if arguments.command == "capability":
        return _capability(arguments)
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


def _capability(arguments: argparse.Namespace) -> int:
    """Install, verify, diagnose, or measure an offline capability pack."""

    action = arguments.action
    try:
        if action == "status":
            runtime = CapabilityRuntime(
                model_root=arguments.model_root,
                profile=arguments.profile,
                low_memory=arguments.low_memory,
            )
            print(json.dumps(runtime.status(), ensure_ascii=False, indent=2))
            return 0
        if action == "manifests":
            if arguments.write is None:
                print(
                    json.dumps(
                        {
                            "directory": CAPABILITY_MANIFEST_DIRECTORY,
                            "note": (
                                "run with --write <dir> to refresh the committed "
                                "manifests; the test suite fails when they drift"
                            ),
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            print(
                json.dumps(
                    write_capability_manifests(arguments.write),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if action == "baseline":
            record = capability_baseline(
                arguments.profile or CapabilityRuntime().profile,
                model_root=arguments.model_root,
                index_directory=arguments.index_dir,
                **({"query": arguments.query} if arguments.query else {}),
            )
            if arguments.output is not None:
                record = {**record, "written": write_run_record(arguments.output, record)}
            print(json.dumps(record, ensure_ascii=False, indent=2))
            return 0
        if action == "doctor":
            bundle = (
                OfflineBundle.read(arguments.bundle)
                if arguments.bundle is not None
                else None
            )
            report = capability_doctor(
                packs=arguments.packs,
                model_root=arguments.model_root,
                bundle=bundle,
            )
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 0 if report["status"] == "ready" else 1
        if action == "uninstall":
            removed = uninstall_capability(
                arguments.pack,
                model_root=arguments.model_root,
                confirmed=arguments.confirm,
            )
            print(json.dumps(removed, ensure_ascii=False, indent=2))
            return 0
        bundle = OfflineBundle.read(arguments.bundle)
        if action == "plan":
            plan = bundle.plan(arguments.pack, model_root=arguments.model_root)
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0 if plan["status"] == "ready" else 1
        if action == "install":
            installed = bundle.install(
                arguments.pack,
                model_root=arguments.model_root,
                confirmed=arguments.confirm,
                apply_python=arguments.apply_python,
            )
            print(json.dumps(installed, ensure_ascii=False, indent=2))
            return 0 if installed["status"] in {"installed", "confirmation_required"} else 1
        if action == "verify":
            packs = arguments.packs or sorted(bundle.packs)
            reports = [bundle.verify(name) for name in packs]
            print(json.dumps(reports, ensure_ascii=False, indent=2))
            return 0 if all(item["status"] == "verified" for item in reports) else 1
    except BundleError as error:
        print(f"error: {error.reason}: {error.detail}", file=sys.stderr)
        return 1
    raise ValueError(f"unknown capability action {action!r}")


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
