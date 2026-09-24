"""Render the committed corpus images that the evaluation samples annotate.

Development-only helper. The corpus itself never imports Pillow: it ships the
resulting PNG bytes under ``evaluation/corpora/<split>/assets/`` and pins each
one by sha256 inside the manifest, so materializing a corpus stays standard
library only and an edited picture fails loudly instead of silently drifting
away from its annotation.

The row geometry is deliberately plain: one text block per row, generous
leading, and the arrow glyph alone in its own row. That is what the arrow rule
in ``game_design_knowledge.layout`` expects, and it is what the deployed core
engine (RapidOCR) can actually transcribe - see ``evaluation/annotation-guide.md``
for the measurements behind the shapes used here.

Usage::

    .venv\\Scripts\\python.exe tools\\render_corpus_assets.py            # write the PNGs
    .venv\\Scripts\\python.exe tools\\render_corpus_assets.py --check    # verify only
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path
from typing import Any, Iterable, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EVALUATION_ROOT = REPOSITORY_ROOT / "evaluation" / "corpora"

WIDTH = 520
LEFT_MARGIN = 60
FIRST_ROW_Y = 36
ROW_LEADING = 44

#: Row font sizes: body text and the arrow block the reading-order ruleset needs.
TEXT_PIXELS = 34
ARROW_PIXELS = 56

FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    r"C:\Windows\Fonts\simsun.ttc",
)


def _row(text: str, pixels: int = TEXT_PIXELS) -> tuple[str, int]:
    return (text, pixels)


ARROW = _row("↑↑", ARROW_PIXELS)

#: Every committed corpus image, in render order.
ASSETS: tuple[tuple[str, str, tuple[tuple[str, int], ...]], ...] = (
    (
        "development_set",
        "战斗流程.png",
        (
            _row("战斗流程"),
            _row("结算"),
            ARROW,
            _row("判定"),
            ARROW,
            _row("开始"),
        ),
    ),
    (
        "development_set",
        "连招流程.png",
        (
            _row("连招流程"),
            _row("起手→追击→收招"),
        ),
    ),
    (
        "golden_set",
        "结算流程.png",
        (
            _row("结算流程"),
            _row("发奖"),
            ARROW,
            _row("结算"),
        ),
    ),
    (
        "golden_set",
        "结算发奖流程.png",
        (
            _row("结算流程"),
            _row("结算→发奖"),
        ),
    ),
)


def _load_font(pixels: int) -> Any:
    from PIL import ImageFont

    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return ImageFont.truetype(candidate, pixels)
    raise SystemExit(
        "No CJK font found in "
        + ", ".join(FONT_CANDIDATES)
        + "; install one or adjust FONT_CANDIDATES."
    )


def _height(rows: Sequence[tuple[str, int]]) -> int:
    return FIRST_ROW_Y + sum(pixels + ROW_LEADING for _, pixels in rows) + 20


def render(rows: Sequence[tuple[str, int]]) -> bytes:
    """Draw one image and return its PNG bytes."""

    import io

    from PIL import Image, ImageDraw

    image = Image.new("RGB", (WIDTH, _height(rows)), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    y = FIRST_ROW_Y
    for text, pixels in rows:
        draw.text((LEFT_MARGIN, y), text, fill=(0, 0, 0), font=_load_font(pixels))
        y += pixels + ROW_LEADING
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _targets() -> Iterable[tuple[Path, bytes]]:
    for split, name, rows in ASSETS:
        yield EVALUATION_ROOT / split / "assets" / name, render(rows)


def _report(line: str) -> None:
    """Print one status line, surviving a stdout that cannot encode it.

    The corpus paths carry Chinese directory names, so a redirected non-UTF-8
    console (Windows CI, ``> file``) would raise ``UnicodeEncodeError`` instead
    of reporting. Fall back to unambiguous ASCII escapes there.
    """

    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        line.encode(encoding)
    except (LookupError, UnicodeEncodeError):
        line = line.encode("ascii", "backslashreplace").decode("ascii")
    print(line)


def main(argv: Sequence[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="只校验已提交的图片与当前渲染是否一致，不写文件",
    )
    arguments = parser.parse_args(argv)

    exit_code = 0
    for path, payload in _targets():
        digest = hashlib.sha256(payload).hexdigest()
        if arguments.check:
            if not path.is_file():
                _report(f"MISSING {path.relative_to(REPOSITORY_ROOT)}")
                exit_code = 1
                continue
            committed = hashlib.sha256(path.read_bytes()).hexdigest()
            status = "ok" if committed == digest else "DIFFERENT"
            if committed != digest:
                exit_code = 1
            _report(f"{status:9} {path.relative_to(REPOSITORY_ROOT)} {committed}")
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        _report(f"wrote     {path.relative_to(REPOSITORY_ROOT)} {digest}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
