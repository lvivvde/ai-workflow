from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
import shutil
import threading
from typing import Iterable

from .cli import _build_index_atomically


SUPPORTED_EXTENSIONS = {".docx", ".xlsx"}
DESTINATION_ROOTS = {
    "docs": Path("docs"),
    "examples": Path("examples") / "sample-corpus",
}
OPERATIONS = {"copy", "move"}
MAX_IMPORT_FILES = 100
FORBIDDEN_PROJECT_PARTS = {".git", ".index", ".venv"}
_IMPORT_LOCK = threading.Lock()


def plan_document_import(
    source_paths: Iterable[str],
    project_root: Path,
    destination: str,
    operation: str,
) -> dict[str, object]:
    project_root = project_root.resolve()
    paths = list(source_paths)
    if not paths:
        raise ValueError("source_paths must contain at least one file")
    if len(paths) > MAX_IMPORT_FILES:
        raise ValueError(f"source_paths cannot contain more than {MAX_IMPORT_FILES} files")
    if destination not in DESTINATION_ROOTS:
        raise ValueError("destination must be 'docs' or 'examples'")
    if operation not in OPERATIONS:
        raise ValueError("operation must be 'copy' or 'move'")

    items: list[dict[str, object]] = []
    seen_destinations: set[Path] = set()
    for raw_path in paths:
        requested_source = Path(raw_path).expanduser()
        if requested_source.is_symlink():
            raise ValueError(f"Symbolic-link imports are not allowed: {requested_source}")
        source = requested_source.resolve()
        if not source.is_file():
            raise FileNotFoundError(f"Import source is not a file: {source}")
        extension = source.suffix.lower()
        if extension not in SUPPORTED_EXTENSIONS:
            raise ValueError(f"Only DOCX and XLSX files can be imported: {source}")

        relative_source = _relative_to_project(source, project_root)
        if relative_source is not None and relative_source.parts:
            if relative_source.parts[0].lower() in FORBIDDEN_PROJECT_PARTS:
                raise ValueError(f"Files cannot be imported from {relative_source.parts[0]}: {source}")

        target_directory = project_root / DESTINATION_ROOTS[destination] / extension[1:]
        target = (target_directory / source.name).resolve()
        if not target.is_relative_to(project_root):
            raise ValueError(f"Import destination escapes the project root: {target}")
        if target in seen_destinations:
            raise ValueError(f"Multiple files resolve to the same destination: {target}")
        seen_destinations.add(target)

        if source == target:
            action = "already_in_place"
        else:
            if target.exists():
                raise FileExistsError(
                    f"Import will not overwrite an existing project file: {target}"
                )
            if relative_source is not None and operation == "copy":
                raise ValueError(
                    "Copying a file already inside the project would index duplicate facts; "
                    f"use operation='move' instead: {source}"
                )
            action = operation

        items.append(
            {
                "source": str(source),
                "destination": str(target),
                "extension": extension,
                "size": source.stat().st_size,
                "sha256": _file_sha256(source),
                "action": action,
            }
        )

    token_payload = {
        "project_root": str(project_root),
        "destination": destination,
        "operation": operation,
        "items": items,
    }
    plan_token = hashlib.sha256(
        json.dumps(token_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "status": "confirmation_required",
        **token_payload,
        "plan_token": plan_token,
        "will_rebuild_shared_index": True,
        "limitations": [
            "只导入 DOCX/XLSX，不解析或自动登记文档中未明确记载的玩法别名。",
            "目标文件已存在时拒绝覆盖。",
        ],
    }


def apply_document_import(
    source_paths: Iterable[str],
    project_root: Path,
    index_directory: Path,
    destination: str,
    operation: str,
    plan_token: str,
) -> dict[str, object]:
    if not plan_token.strip():
        raise ValueError("plan_token is required")
    project_root = project_root.resolve()
    index_directory = index_directory.resolve()
    if not index_directory.is_relative_to(project_root):
        raise ValueError("The shared index must be inside GAME_DESIGN_PROJECT_ROOT")

    if not _IMPORT_LOCK.acquire(blocking=False):
        raise RuntimeError("Another document import or index rebuild is already running")
    try:
        plan = plan_document_import(
            source_paths, project_root, destination, operation
        )
        if not hmac.compare_digest(str(plan["plan_token"]), plan_token):
            raise ValueError(
                "Import plan changed; call plan_document_import again and reconfirm it"
            )

        completed_actions: list[tuple[str, Path, Path]] = []
        try:
            for item in plan["items"]:
                source = Path(str(item["source"]))
                target = Path(str(item["destination"]))
                action = str(item["action"])
                if action == "already_in_place":
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if action == "copy":
                    shutil.copy2(source, target)
                else:
                    shutil.move(source, target)
                completed_actions.append((action, source, target))

            index_report = _build_index_atomically(project_root, index_directory)
        except BaseException as error:
            rollback_errors = _rollback_import(completed_actions)
            if rollback_errors:
                raise RuntimeError(
                    f"Import failed and rollback was incomplete: {rollback_errors}"
                ) from error
            raise

        return {
            "status": "completed",
            "destination": destination,
            "operation": operation,
            "files": plan["items"],
            "index_directory": str(index_directory),
            "index_report": index_report,
            "git_paths_to_commit": [
                str(project_root / DESTINATION_ROOTS[destination]),
                str(index_directory),
            ],
        }
    finally:
        _IMPORT_LOCK.release()


def rebuild_shared_index(project_root: Path, index_directory: Path) -> dict[str, object]:
    project_root = project_root.resolve()
    index_directory = index_directory.resolve()
    if not index_directory.is_relative_to(project_root):
        raise ValueError("The shared index must be inside GAME_DESIGN_PROJECT_ROOT")
    if not _IMPORT_LOCK.acquire(blocking=False):
        raise RuntimeError("Another document import or index rebuild is already running")
    try:
        return {
            "status": "completed",
            "project_root": str(project_root),
            "index_directory": str(index_directory),
            "index_report": _build_index_atomically(project_root, index_directory),
            "git_paths_to_commit": [str(index_directory)],
        }
    finally:
        _IMPORT_LOCK.release()


def _rollback_import(actions: list[tuple[str, Path, Path]]) -> list[str]:
    errors: list[str] = []
    for action, source, target in reversed(actions):
        try:
            if action == "copy":
                target.unlink(missing_ok=True)
            else:
                source.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(target, source)
        except OSError as error:
            errors.append(f"{target}: {error}")
    return errors


def _relative_to_project(path: Path, project_root: Path) -> Path | None:
    try:
        return path.relative_to(project_root)
    except ValueError:
        return None


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
