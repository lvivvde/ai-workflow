"""Stubs that decide "which machine is this" in the test, not on the host.

Tests that assert the degradation path ("no OCR engine is installed here")
have to describe that machine themselves. Otherwise they quietly assert
whatever the developer's machine happens to have, and pass or fail by luck.

Two seams cover every shape these tests take:

- :func:`no_ocr_engine_installed` replaces the single host probe the
  degradation chain reads (``OcrEngine.installed``), for in-process runs.
- :data:`OCR_FREE_INTERPRETER_FLAGS` plus :func:`ocr_free_environment` do the
  same for the CLI and MCP tests, which build their index in a real
  subprocess: ``-S`` keeps site-packages off ``sys.path``, so the optional
  OCR distributions are not importable, and an empty ``PATH`` hides the
  ``tesseract`` executable.
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
from typing import Iterator
from unittest import mock

from game_design_knowledge.ocr import OcrEngine

OCR_FREE_INTERPRETER_FLAGS: tuple[str, ...] = ("-S",)
"""Interpret the run as having no third-party packages installed."""


@contextmanager
def no_ocr_engine_installed() -> Iterator[None]:
    """Run the block as if no OCR engine were installed on this machine."""

    with mock.patch.object(OcrEngine, "installed", lambda self: False):
        yield


def ocr_free_environment(project_root: Path) -> dict[str, str]:
    """The environment of this checkout on a machine with no OCR engine."""

    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(project_root) / "src")
    environment["PATH"] = ""
    return environment
