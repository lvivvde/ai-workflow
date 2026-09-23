"""Corpus images have to be real, pinned, and on disk where the labels say.

The failure this file exists for: a corpus whose labels describe a picture that
the pixels do not contain. That is how ``ocr_transcription`` and the reading
order layers stayed unmeasurable while looking annotated, so every path that
could bring it back is pinned here.
"""

from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile

from game_design_knowledge.evaluation import SchemaError, load_corpus
from game_design_knowledge.evaluation.fixtures import (
    TINY_PNG,
    materialize_document,
)
from game_design_knowledge.evaluation.schema import DocumentSpec


PROJECT_ROOT = Path(__file__).parents[1]
CORPORA_ROOT = PROJECT_ROOT / "evaluation" / "corpora"
COMMITTED_CORPORA = ("v1_compatibility", "development_set", "golden_set")
RENDERER_PATH = PROJECT_ROOT / "tools" / "render_corpus_assets.py"

#: Enough of a PNG to pass the signature check without carrying real pixels.
FAKE_PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic corpus pixels"


def _write_asset(root: Path, name: str, payload: bytes = FAKE_PNG) -> str:
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(payload)
    return name


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _renderer_rows() -> dict[tuple[str, str], list[str]]:
    """The rows the renderer draws, keyed by (corpus directory, asset name)."""

    spec = importlib.util.spec_from_file_location("render_corpus_assets", RENDERER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return {
        (split, name): [text for text, _ in rows]
        for split, name, rows in module.ASSETS
    }


class AssetGeneratorTests(unittest.TestCase):
    def materialize(
        self, generator: dict[str, object], *, root: Path, asset_root: Path | None
    ) -> Path:
        document = DocumentSpec(
            path=str(generator.get("path") or "图像/流程.png"),
            generator=generator,
            asset_root=asset_root,
        )
        destination = root / "out"
        return materialize_document(document, destination)

    def test_a_png_document_writes_the_asset_it_points_at(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            name = _write_asset(root, "assets/流程.png")
            target = self.materialize(
                {
                    "kind": "png",
                    "asset": name,
                    "asset_sha256": _sha256(FAKE_PNG),
                },
                root=root,
                asset_root=root,
            )

            self.assertEqual(target.read_bytes(), FAKE_PNG)

    def test_a_png_document_without_an_asset_stays_the_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = self.materialize({"kind": "png"}, root=root, asset_root=root)

            self.assertEqual(target.read_bytes(), TINY_PNG)

    def test_a_docx_image_block_embeds_the_asset_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            name = _write_asset(root, "assets/连招.png")
            target = self.materialize(
                {
                    "kind": "docx",
                    "blocks": [
                        {"type": "heading", "level": 1, "text": "连招与记法"},
                        {
                            "type": "image",
                            "relationship_id": "rIdImage1",
                            "media_name": "word/media/flow.png",
                            "asset": name,
                            "asset_sha256": _sha256(FAKE_PNG),
                        },
                    ],
                },
                root=root,
                asset_root=root,
            )

            with zipfile.ZipFile(target) as archive:
                self.assertEqual(
                    archive.read("word/media/flow.png"), FAKE_PNG
                )

    def test_a_missing_asset_names_the_file_it_wanted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(SchemaError) as context:
                self.materialize(
                    {
                        "kind": "png",
                        "asset": "assets/缺席.png",
                        "asset_sha256": _sha256(FAKE_PNG),
                    },
                    root=root,
                    asset_root=root,
                )

            self.assertIn("assets/缺席.png", str(context.exception))
            self.assertIn("missing", str(context.exception))

    def test_an_asset_that_drifted_from_its_pin_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            name = _write_asset(root, "assets/流程.png")
            with self.assertRaises(SchemaError) as context:
                self.materialize(
                    {"kind": "png", "asset": name, "asset_sha256": "0" * 64},
                    root=root,
                    asset_root=root,
                )

            self.assertIn("sha256", str(context.exception))
            self.assertIn(_sha256(FAKE_PNG), str(context.exception))

    def test_an_asset_may_not_escape_the_corpus(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            outside = root.parent / "outside.png"
            outside.write_bytes(FAKE_PNG)
            try:
                with self.assertRaises(SchemaError) as context:
                    self.materialize(
                        {
                            "kind": "png",
                            "asset": "../outside.png",
                            "asset_sha256": _sha256(FAKE_PNG),
                        },
                        root=root,
                        asset_root=root,
                    )

                self.assertIn("inside the corpus", str(context.exception))
            finally:
                outside.unlink()

    def test_an_asset_needs_the_corpus_directory_it_belongs_to(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            name = _write_asset(root, "assets/流程.png")
            with self.assertRaises(SchemaError) as context:
                self.materialize(
                    {
                        "kind": "png",
                        "asset": name,
                        "asset_sha256": _sha256(FAKE_PNG),
                    },
                    root=root,
                    asset_root=None,
                )

            self.assertIn("load_corpus", str(context.exception))

    def test_an_asset_that_is_not_a_png_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            name = _write_asset(root, "assets/流程.png", b"BM\x00\x00 not a png")
            with self.assertRaises(SchemaError) as context:
                self.materialize(
                    {
                        "kind": "png",
                        "asset": name,
                        "asset_sha256": _sha256(b"BM\x00\x00 not a png"),
                    },
                    root=root,
                    asset_root=root,
                )

            self.assertIn("must be a PNG", str(context.exception))

    def test_an_unpinned_asset_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            name = _write_asset(root, "assets/流程.png")
            with self.assertRaises(SchemaError) as context:
                self.materialize(
                    {"kind": "png", "asset": name}, root=root, asset_root=root
                )

            self.assertIn("sha256", str(context.exception))

    def test_a_pin_without_an_asset_is_refused(self) -> None:
        """A pin with nothing to pin would silently leave the placeholder in place."""

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaises(SchemaError) as context:
                self.materialize(
                    {"kind": "png", "asset_sha256": _sha256(FAKE_PNG)},
                    root=root,
                    asset_root=root,
                )

            self.assertIn("asset_sha256 without asset", str(context.exception))

    def test_a_docx_image_block_without_an_asset_stays_the_placeholder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            target = self.materialize(
                {
                    "kind": "docx",
                    "blocks": [
                        {
                            "type": "image",
                            "relationship_id": "rIdImage1",
                            "media_name": "word/media/flow.png",
                        }
                    ],
                },
                root=root,
                asset_root=root,
            )

            with zipfile.ZipFile(target) as archive:
                self.assertEqual(archive.read("word/media/flow.png"), TINY_PNG)


class CommittedCorpusAssetTests(unittest.TestCase):
    """The committed corpora must agree with the pictures they ship."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.corpora = [
            load_corpus(CORPORA_ROOT / name) for name in COMMITTED_CORPORA
        ]

    def _image_generators(self):
        for corpus in self.corpora:
            for sample in corpus.samples:
                for document in sample.documents:
                    generator = document.generator
                    if generator.get("asset"):
                        yield corpus, sample, document.path, generator
                        continue
                    for block in generator.get("blocks") or ():
                        if block.get("asset"):
                            yield corpus, sample, document.path, block

    def test_every_referenced_asset_exists_and_matches_its_pin(self) -> None:
        seen = 0
        for corpus, sample, document_path, holder in self._image_generators():
            with self.subTest(corpus=corpus.manifest.name, sample=sample.sample_id):
                asset = corpus.root / str(holder["asset"])
                self.assertTrue(
                    asset.is_file(),
                    f"{sample.sample_id} points at a missing picture: {asset}",
                )
                self.assertEqual(
                    hashlib.sha256(asset.read_bytes()).hexdigest(),
                    holder["asset_sha256"],
                    f"{document_path} shipped different pixels than it pinned",
                )
                seen += 1

        self.assertGreater(seen, 0, "no committed corpus ships a real picture")

    def test_every_annotated_transcription_is_the_join_of_its_regions(self) -> None:
        checked = 0
        for corpus in self.corpora:
            for sample in corpus.samples:
                transcription = sample.expected.transcription
                if transcription is None or not transcription.regions:
                    continue
                with self.subTest(corpus=corpus.manifest.name, sample=sample.sample_id):
                    self.assertEqual(
                        transcription.text,
                        " ".join(transcription.regions),
                        "the reference transcription and the annotated regions "
                        "describe different pictures",
                    )
                    checked += 1

        self.assertGreater(checked, 0, "no committed corpus annotates a transcription")

    def test_the_renderer_draws_exactly_the_regions_the_labels_annotate(self) -> None:
        """The picture is the renderer's output, so the rows must be the labels."""

        rows_by_asset = _renderer_rows()
        checked = 0
        for corpus, sample, document_path, holder in self._image_generators():
            transcription = sample.expected.transcription
            if transcription is None:
                continue
            key = (corpus.root.name, Path(str(holder["asset"])).name)
            with self.subTest(corpus=corpus.manifest.name, sample=sample.sample_id):
                self.assertIn(
                    key,
                    rows_by_asset,
                    f"{document_path} points at a picture the renderer does not draw",
                )
                self.assertEqual(
                    rows_by_asset[key],
                    list(transcription.regions),
                    "the annotated regions are not the rows in the picture",
                )
                checked += 1

        self.assertGreater(checked, 0, "no annotated sample ships a real picture")


@unittest.skipUnless(
    importlib.util.find_spec("PIL") is not None,
    "rendering the committed corpus pictures needs Pillow (development only)",
)
class RendererAgreementTests(unittest.TestCase):
    def test_the_committed_pictures_are_what_the_renderer_produces(self) -> None:
        result = subprocess.run(
            [
                sys.executable,
                str(PROJECT_ROOT / "tools" / "render_corpus_assets.py"),
                "--check",
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("DIFFERENT", result.stdout)


if __name__ == "__main__":  # pragma: no cover - unittest discovers this file
    unittest.main()
