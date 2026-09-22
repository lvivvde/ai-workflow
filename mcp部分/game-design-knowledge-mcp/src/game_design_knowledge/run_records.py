"""Resource budgets per Hardware Profile, and the records a baseline writes.

A profile is a promise about concurrency and residency; a run record is how
that promise is checked on a machine. Every baseline run writes one record with
the latency it took, the peak memory it reached, the disk it used, and every
degradation event that happened while it ran, so "it worked" is a measurement
rather than an impression.

The budget for a profile lives here and nowhere else. The capability runtime
reads it instead of repeating the numbers, so a profile cannot drift between
what is documented and what is applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import sys
from time import perf_counter
from typing import Any, Callable, Mapping


RUN_RECORD_VERSION = "capability-run-v1"

#: What each Hardware Profile promises. ``baseline`` is the smallest machine
#: the project supports, ``recommended`` is a normal workstation, and
#: ``visual`` adds on-demand vision models to that.
PROFILE_BUDGETS: Mapping[str, Mapping[str, Any]] = {
    "baseline": {
        "ocr_concurrency": 1,
        "heavy_jobs_concurrent": 1,
        "native_parsing_concurrent": True,
        "ocr_batch_size": 4,
        "idle_timeout_seconds": 120.0,
        "min_ram_gb": 8.0,
        "vector_recall": "disabled",
        "visual_models": "not_installed",
    },
    "recommended": {
        "ocr_concurrency": 2,
        "heavy_jobs_concurrent": 1,
        "native_parsing_concurrent": True,
        "ocr_batch_size": 8,
        "idle_timeout_seconds": 300.0,
        "min_ram_gb": 16.0,
        "vector_recall": "disabled",
        "visual_models": "not_installed",
    },
    "visual": {
        "ocr_concurrency": 2,
        "heavy_jobs_concurrent": 1,
        "native_parsing_concurrent": True,
        "ocr_batch_size": 8,
        "idle_timeout_seconds": 300.0,
        "min_ram_gb": 16.0,
        "vector_recall": "disabled",
        "visual_models": "on_demand",
    },
}

DEFAULT_BASELINE_QUERY = "玩家每日可参与5次。"


def profile_budget(profile: str) -> dict[str, Any]:
    """The concurrency and residency a profile applies, as its own copy."""

    try:
        budget = PROFILE_BUDGETS[profile]
    except KeyError as error:
        raise KeyError(
            f"unknown Hardware Profile {profile!r}; "
            f"known profiles are {sorted(PROFILE_BUDGETS)}"
        ) from error
    return dict(budget)


def peak_memory_bytes() -> int | None:
    """The largest working set this process has reached, when measurable."""

    return _platform_memory()[1]


def memory_bytes() -> int | None:
    """The working set this process holds right now, when measurable."""

    return _platform_memory()[0]


def disk_usage(path: Path | None = None) -> dict[str, Any]:
    """Free, used, and total space around a path, in GB."""

    target = _existing_ancestor(Path(path) if path is not None else Path.cwd())
    try:
        usage = shutil.disk_usage(target)
    except OSError as error:
        return {
            "path": str(target),
            "available": False,
            "error": str(error),
            "total_gb": None,
            "used_gb": None,
            "free_gb": None,
        }
    gib = 1024**3
    return {
        "path": str(target),
        "available": True,
        "error": "",
        "total_gb": round(usage.total / gib, 2),
        "used_gb": round(usage.used / gib, 2),
        "free_gb": round(usage.free / gib, 2),
    }


@dataclass
class RunRecorder:
    """One measured run, opened before the work and closed after it.

    The record is finished by ``finish()`` (or by leaving the context
    manager), so a caller can write it even when the work raised: a failed run
    is a record too, and it keeps the degradations that happened before the
    failure.
    """

    profile: str
    operation: str
    model_root: Path | None = None
    index_directory: Path | None = None
    hardware: Mapping[str, Any] | None = None
    clock: Callable[[], float] = perf_counter
    wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    degradation_events: list[dict[str, Any]] = field(default_factory=list)
    notes: dict[str, Any] = field(default_factory=dict)
    status: str = "running"
    error: str = ""
    started_at: str = ""
    finished_at: str = ""
    _started: float = 0.0
    _finished: float | None = None
    _disk_before: dict[str, Any] | None = None
    _disk_after: dict[str, Any] | None = None
    _memory_before: int | None = None
    _memory_after: int | None = None
    _peak: int | None = None

    def start(self) -> "RunRecorder":
        profile_budget(self.profile)
        self._started = self.clock()
        self.started_at = _stamp(self.wall_clock())
        self._disk_before = self._disk()
        self._memory_before = memory_bytes()
        self.status = "running"
        return self

    def add_degradation(
        self, channel: str, reason: str, detail: str = "", **extra: Any
    ) -> None:
        entry = {
            "channel": str(channel),
            "reason": str(reason),
            "detail": str(detail),
            "at": self.started_at or _stamp(self.wall_clock()),
        }
        entry.update({key: value for key, value in extra.items()})
        self.degradation_events.append(entry)

    def note(self, key: str, value: Any) -> None:
        self.notes[str(key)] = value

    def finish(self, status: str | None = None, error: str = "") -> dict[str, Any]:
        if self._finished is None:
            self._finished = self.clock()
            self.finished_at = _stamp(self.wall_clock())
        self._disk_after = self._disk()
        self._memory_after = memory_bytes()
        self._peak = peak_memory_bytes()
        if status:
            self.status = status
        if error:
            self.error = error
            self.status = "failed"
        elif self.status == "running":
            self.status = "succeeded"
        return self.as_payload()

    def as_payload(self) -> dict[str, Any]:
        finished = self._finished if self._finished is not None else self.clock()
        peak = self._peak if self._peak is not None else peak_memory_bytes()
        return {
            "record_version": RUN_RECORD_VERSION,
            "operation": self.operation,
            "profile": self.profile,
            "budget": profile_budget(self.profile),
            "status": self.status,
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "latency_seconds": round(max(finished - self._started, 0.0), 3),
            "peak_memory_bytes": peak,
            "peak_memory_mb": (
                round(peak / (1024**2), 1) if peak is not None else None
            ),
            "memory_bytes_start": self._memory_before,
            "memory_bytes_end": self._memory_after,
            "disk": {
                "model_root": str(self.model_root) if self.model_root else None,
                "index_directory": (
                    str(self.index_directory) if self.index_directory else None
                ),
                "before": self._disk_before,
                "after": self._disk_after,
            },
            "hardware": dict(self.hardware) if self.hardware else None,
            "platform": {
                "os": os.name,
                "sys_platform": sys.platform,
                "release": platform.release(),
                "machine": platform.machine(),
                "python": sys.version.split()[0],
            },
            "degradation_events": [dict(event) for event in self.degradation_events],
            "notes": dict(self.notes),
            "boundary": (
                "A run record measures one machine and one profile; a record from "
                "another platform is evidence about that platform only."
            ),
        }

    def __enter__(self) -> "RunRecorder":
        return self.start()

    def __exit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> bool:
        if exc_value is None:
            self.finish()
        else:
            self.finish(error=f"{type(exc_value).__name__}: {exc_value}")
        return False

    # -- internals ---------------------------------------------------------

    def _disk(self) -> dict[str, Any]:
        reference = self.model_root or self.index_directory
        return disk_usage(reference) if reference is not None else disk_usage()


def write_run_record(path: Path, record: Mapping[str, Any]) -> dict[str, Any]:
    """Append one record as a JSON line, creating its directory if needed."""

    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(dict(record), ensure_ascii=False, sort_keys=True)
    with target.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(line + "\n")
    return {"path": str(target), "written": True, "bytes": len(line.encode("utf-8")) + 1}


def read_run_records(path: Path) -> list[dict[str, Any]]:
    """Read the JSONL records one baseline wrote, oldest first."""

    target = Path(path)
    if not target.is_file():
        return []
    records: list[dict[str, Any]] = []
    with target.open("r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as error:
                raise ValueError(f"{target}: line {number} is not a run record") from error
    return records


def capability_baseline(
    profile: str,
    *,
    model_root: Path | None = None,
    index_directory: Path | None = None,
    query: str = DEFAULT_BASELINE_QUERY,
    limit: int = 5,
) -> dict[str, Any]:
    """Run one measured pass on this machine and return its record.

    The pass is deliberately ordinary: detect the packs, load the ones this
    profile uses, and (when an index is given) answer one query. Everything it
    could not do becomes a degradation event, so a bare machine still produces
    a record that says what it was missing.
    """

    from .capabilities import PACKS, CapabilityRuntime
    from .ocr import select_engine

    runtime = CapabilityRuntime(model_root=model_root, profile=profile)
    recorder = RunRecorder(
        profile=profile,
        operation="capability_baseline",
        model_root=model_root,
        index_directory=index_directory,
        hardware=runtime.hardware.as_payload(),
    )
    with recorder:
        status = runtime.status()
        recorder.note("packs", {pack["name"]: pack["status"] for pack in status["packs"]})
        recorder.note("limits", status["limits"])
        for pack in status["packs"]:
            if pack["status"] != "available":
                recorder.add_degradation(
                    pack["name"], pack["status"], pack["reason"]
                )
        loaded: list[str] = []
        for pack in PACKS:
            if not pack.optional or pack.name == "core":
                runtime.acquire(pack)
                loaded.append(pack.name)
        recorder.note("acquired", loaded)
        recorder.note("residency", runtime.residency())

        selection = select_engine()
        recorder.note("ocr", selection.as_payload())
        if selection.execution_status != "succeeded":
            recorder.add_degradation(
                "ocr", selection.reason_code, selection.reason
            )

        if index_directory is None:
            recorder.note("query", {"status": "skipped", "reason": "no index directory"})
        else:
            recorder.note("query", _measure_query(index_directory, query, limit))
        runtime.release_all()
    return recorder.as_payload()


def _measure_query(
    index_directory: Path, query: str, limit: int
) -> dict[str, Any]:
    """Answer one query against an index and report what it cost."""

    from .server import search_evidence

    previous = os.environ.get("GAME_DESIGN_INDEX_DIR")
    os.environ["GAME_DESIGN_INDEX_DIR"] = os.fspath(Path(index_directory))
    started = perf_counter()
    try:
        payload = search_evidence(query, limit=limit)
    finally:
        if previous is None:
            os.environ.pop("GAME_DESIGN_INDEX_DIR", None)
        else:
            os.environ["GAME_DESIGN_INDEX_DIR"] = previous
    return {
        "query": query,
        "status": payload.get("status"),
        "returned": len(payload.get("evidence") or ()),
        "seconds": round(perf_counter() - started, 3),
    }


def _platform_memory() -> tuple[int | None, int | None]:
    """(working set, peak working set) in bytes, or (None, None) if unknown."""

    if os.name == "nt":
        return _windows_memory()
    try:
        import resource
    except ImportError:  # pragma: no cover - Windows without psapi
        return (None, None)
    peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss) * 1024
    return (peak, peak)


def _windows_memory() -> tuple[int | None, int | None]:
    """(working set, peak working set) through psapi, on Windows only.

    The current process is addressed by its pseudo handle, which is ``-1`` as
    a full-width pointer; passing it as a default-width integer would send the
    call a different handle.
    """

    import ctypes

    class MemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = MemoryCounters()
    counters.cb = ctypes.sizeof(MemoryCounters)
    function = None
    try:
        function = ctypes.windll.psapi.GetProcessMemoryInfo
    except (AttributeError, OSError):
        try:
            function = ctypes.windll.kernel32.K32GetProcessMemoryInfo
        except (AttributeError, OSError):
            return (None, None)
    try:
        function.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(MemoryCounters),
            ctypes.c_uint,
        ]
        function.restype = ctypes.c_int
        ok = function(
            ctypes.c_void_p(-1), ctypes.byref(counters), ctypes.sizeof(counters)
        )
    except (AttributeError, OSError, ValueError):
        return (None, None)
    if not ok:
        return (None, None)
    return (int(counters.WorkingSetSize), int(counters.PeakWorkingSetSize))


def _existing_ancestor(path: Path) -> Path:
    target = path
    while not target.exists() and target.parent != target:
        target = target.parent
    return target


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


__all__ = [
    "DEFAULT_BASELINE_QUERY",
    "PROFILE_BUDGETS",
    "RUN_RECORD_VERSION",
    "RunRecorder",
    "capability_baseline",
    "disk_usage",
    "memory_bytes",
    "peak_memory_bytes",
    "profile_budget",
    "read_run_records",
    "write_run_record",
]
