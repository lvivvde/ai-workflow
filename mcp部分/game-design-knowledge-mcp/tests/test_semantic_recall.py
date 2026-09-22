"""V2-10: the optional vector channel, and everything it may not do.

The deterministic retrieval base is measured in ``test_retrieval``. These
questions are about the optional layer on top of it: is the provider local and
deterministic, does a vector record stay bound to one model identity, is a
vector hit read back before it is shown, is a weak hit kept out of the answer,
and does the whole channel disappear without taking the facts with it.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile

from game_design_knowledge import query_rewrite as rewrite
from game_design_knowledge.embeddings import (
    DIMENSION_MISMATCH,
    EMBEDDING_PROVIDER_ENV,
    PROVIDER_INVALID,
    PROVIDER_NOT_CONFIGURED,
    EmbeddingUnavailable,
    HashingTextEmbedding,
    cosine_similarity,
    identity_of,
    load_provider,
)
from game_design_knowledge.index_build import build_index_atomically
from game_design_knowledge.retrieval import (
    GENERATED_CHANNEL,
    SEMANTIC_CANDIDATE,
    SEMANTIC_CHANNEL,
    SEMANTIC_MODE_BOUNDARY,
    WEAK_SEMANTIC_BOUNDARY,
    retrieve,
)
from game_design_knowledge.semantic_index import (
    DEFAULT_SIMILARITY_THRESHOLD,
    INDEX_MISSING,
    INDEX_STALE,
    MODEL_CHANGED,
    SEMANTIC_INDEX_FILENAME,
    SEMANTIC_SCHEMA_VERSION,
    STATUS_MISSING,
    STATUS_READY,
    STATUS_STALE,
    SemanticChannel,
    SemanticIndex,
)
from game_design_knowledge.shared_index import SharedIndexRead, index_status_for_database


CATALOG = {
    "version": 1,
    "features": [
        {
            "key": "lucky-wheel",
            "canonical_name": "幸运转盘",
            "source": "knowledge/catalog.json",
            "aliases": [
                {
                    "name": "转盘",
                    "source": "knowledge/catalog.json",
                    "confirmed_at": "2026-08-09",
                    "confirmed_by": "测试策划",
                }
            ],
        }
    ],
}


class SemanticModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="gdk-semantic-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "project"

    # -- provider ---------------------------------------------------------

    def test_the_reference_provider_is_local_deterministic_and_named(self) -> None:
        first = HashingTextEmbedding(64)
        second = HashingTextEmbedding(64)
        rows = first.embed(["幸运转盘", "幸运转盘", "玩家每日可参与5次。"])

        self.assertEqual(rows[0], rows[1])
        self.assertEqual(rows[0], second.embed(["幸运转盘"])[0])
        self.assertEqual(len(rows[0]), 64)
        self.assertNotEqual(rows[0], rows[2])
        # A vector is unit length, so a cosine is a plain dot product.
        self.assertAlmostEqual(cosine_similarity(rows[0], rows[0]), 1.0, places=6)

        self.assertEqual(identity_of(first).as_payload(), {
            "model_id": "local-hashing-text",
            "model_version": "1",
            "dimension": 64,
        })
        with self.assertRaises(EmbeddingUnavailable) as caught:
            identity_of(object())
        self.assertEqual(caught.exception.reason, PROVIDER_INVALID)

    def test_the_switch_is_explicit_and_loaded_from_the_environment(self) -> None:
        with self.assertRaises(EmbeddingUnavailable) as caught:
            load_provider({})
        self.assertEqual(caught.exception.reason, PROVIDER_NOT_CONFIGURED)

        configured = load_provider({EMBEDDING_PROVIDER_ENV: "hashing:32"})
        self.assertEqual(identity_of(configured).dimension, 32)

        imported = load_provider(
            {
                EMBEDDING_PROVIDER_ENV: (
                    "game_design_knowledge.embeddings:HashingTextEmbedding"
                )
            }
        )
        self.assertEqual(identity_of(imported).model_id, "local-hashing-text")

        for broken in ("nope:missing", "hashing:abc", "just-a-name"):
            with self.assertRaises(EmbeddingUnavailable):
                load_provider({EMBEDDING_PROVIDER_ENV: broken})

    # -- query rewriting --------------------------------------------------

    def test_a_variant_that_moves_a_value_unit_time_or_negation_is_refused(
        self,
    ) -> None:
        cases = [
            ("每日可参与5次。", "每天可参与5次。", True, ""),
            ("每日可参与5次。", "每日可参与30次。", False, "changes_value"),
            ("冷却30秒", "冷却30分钟", False, "changes_unit"),
            ("v2 规则说明", "v3 规则说明", False, "changes_version"),
            ("20:00 开启活动", "21:00 开启活动", False, "changes_time"),
            ("不允许跳过动画", "允许跳过动画", False, "changes_negation"),
            ("仅限本服开启", "全服开启", False, "changes_scope"),
            ("幸运转盘", "幸运转盘", False, "identical_to_original"),
            ("规则", "", False, "empty_variant"),
            ("规则", "规" * (rewrite.MAX_VARIANT_CHARACTERS + 1), False, "variant_too_long"),
        ]
        for original, variant, accepted, reason in cases:
            with self.subTest(variant=variant):
                ok, found = rewrite.variant_guard(original, variant)
                self.assertEqual(ok, accepted)
                self.assertEqual(found, reason)

    def test_every_refusal_is_reported_and_the_count_stays_bounded(self) -> None:
        class Rewriter:
            name = "test-rewriter"
            version = "1.2"

            def variants(self, query: str) -> list[str]:
                return [
                    "每天可参与5次。",
                    "每日可参与30次。",
                    "每日能参与5次。",
                    "每日可参与5次吗。",
                    "每日可参与5次。请回答。",
                    "每日可参与5次。谢谢。",
                ]

        accepted, rejected, report = rewrite.generated_variants(
            Rewriter(), "每日可参与5次。"
        )

        self.assertLessEqual(len(accepted), rewrite.MAX_GENERATED_VARIANTS)
        self.assertEqual(report["status"], "applied")
        self.assertEqual(report["name"], "test-rewriter")
        self.assertEqual(report["version"], "1.2")
        self.assertEqual(report["accepted"], len(accepted))
        self.assertTrue(any(item["reason"] == "changes_value" for item in rejected))
        self.assertTrue(
            any(item["reason"] == "variant_limit_reached" for item in rejected),
            rejected,
        )

        class Broken:
            name = "broken"
            version = "0"

            def variants(self, query: str) -> list[str]:
                raise RuntimeError("模型没加载")

        accepted, rejected, report = rewrite.generated_variants(Broken(), "规则")
        self.assertEqual((accepted, rejected), ([], []))
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["reason"], rewrite.REWRITER_FAILED)

        class Noisy:
            name = "noisy"
            version = "0"

            def variants(self, query: str) -> list[str]:
                return ["每日可参与30次。"]

        accepted, rejected, report = rewrite.generated_variants(Noisy(), "每日可参与5次。")
        self.assertEqual(accepted, [])
        self.assertEqual(report["status"], "refused")
        self.assertEqual(rejected[0]["reason"], "changes_value")

    # -- sidecar ----------------------------------------------------------

    def test_a_built_sidecar_binds_units_hashes_and_one_model_identity(self) -> None:
        index_directory = self._project()
        sidecar = SemanticIndex(index_directory / SEMANTIC_INDEX_FILENAME)
        provider = HashingTextEmbedding(64)

        with self._facts(index_directory) as facts:
            report = sidecar.build(facts, provider)
            described = sidecar.describe(facts)

        self.assertEqual(report["status"], "built")
        self.assertGreater(report["vectors"], 0)
        self.assertEqual(described["status"], STATUS_READY)
        self.assertEqual(described["index_version"], SEMANTIC_SCHEMA_VERSION)
        self.assertEqual(described["model_id"], "local-hashing-text")
        self.assertEqual(described["dimension"], 64)
        self.assertEqual(described["namespaces"], ["source_facts"])
        self.assertFalse(described["image_embedding"])

        connection = sqlite3.connect(sidecar.path)
        try:
            rows = connection.execute(
                """
                SELECT unit_id, namespace, source_sha256, input_sha256,
                       model_version, dimension, length(vector) AS size
                FROM semantic_vectors
                """
            ).fetchall()
        finally:
            connection.close()

        self.assertTrue(rows)
        for unit_id, namespace, source_sha256, input_sha256, version, size_dim, size in rows:
            self.assertTrue(str(unit_id).startswith("evidence:"))
            self.assertFalse(str(unit_id).startswith("image:"))
            self.assertEqual(namespace, "source_facts")
            self.assertEqual(len(source_sha256), 64)
            self.assertEqual(len(input_sha256), 64)
            self.assertEqual(version, "1")
            self.assertEqual(size_dim, 64)
            self.assertEqual(size, 4 * 64)

    def test_a_sidecar_from_another_model_or_dimension_is_never_searched(self) -> None:
        index_directory = self._project()
        sidecar = SemanticIndex(index_directory / SEMANTIC_INDEX_FILENAME)
        with self._facts(index_directory) as facts:
            sidecar.build(facts, HashingTextEmbedding(64))

            class OtherVersion(HashingTextEmbedding):
                model_version = "2"

            class OtherDimension(HashingTextEmbedding):
                def __init__(self) -> None:
                    super().__init__(32)

            moved = sidecar.search(facts, OtherVersion(64), "幸运转盘")
            resized = sidecar.search(facts, OtherDimension(), "幸运转盘")

        self.assertEqual(moved["status"], "incompatible")
        self.assertEqual(moved["reason"], MODEL_CHANGED)
        self.assertEqual(moved["hits"], [])
        self.assertEqual(resized["status"], "incompatible")
        self.assertEqual(resized["reason"], MODEL_CHANGED)

    def test_a_sidecar_whose_sources_moved_on_is_reported_stale(self) -> None:
        index_directory = self._project()
        sidecar = SemanticIndex(index_directory / SEMANTIC_INDEX_FILENAME)
        with self._facts(index_directory) as facts:
            sidecar.build(facts, HashingTextEmbedding(64))

        _write_docx(
            self.root / "docs/活动系统.docx",
            [
                ("Heading1", "活动系统"),
                ("Heading2", "幸运转盘"),
                (None, "玩家每日可参与8次。"),
            ],
        )
        build_index_atomically(self.root, index_directory)

        with self._facts(index_directory) as facts:
            described = sidecar.describe(facts)
            result = sidecar.search(facts, HashingTextEmbedding(64), "幸运转盘")

        self.assertEqual(described["status"], STATUS_STALE)
        self.assertGreater(described["stale_vectors"], 0)
        self.assertEqual(result["status"], "stale")
        self.assertEqual(result["reason"], INDEX_STALE)
        self.assertEqual(result["hits"], [])

    def test_dropping_the_sidecar_leaves_the_facts_index_readable(self) -> None:
        index_directory = self._project()
        sidecar = SemanticIndex(index_directory / SEMANTIC_INDEX_FILENAME)
        with self._facts(index_directory) as facts:
            sidecar.build(facts, HashingTextEmbedding(64))
            removed = sidecar.drop()
            described = sidecar.describe(facts)
            result = sidecar.search(facts, HashingTextEmbedding(64), "幸运转盘")
            status = index_status_for_database(index_directory / "knowledge.sqlite")

        self.assertEqual(removed["status"], "dropped")
        self.assertFalse(sidecar.exists())
        self.assertEqual(described["status"], STATUS_MISSING)
        self.assertEqual(result["status"], "missing")
        self.assertEqual(result["reason"], INDEX_MISSING)
        self.assertFalse(status["is_stale"])
        connection = sqlite3.connect(index_directory / "knowledge.sqlite")
        try:
            counted = connection.execute(
                "SELECT COUNT(*) FROM evidence"
            ).fetchone()[0]
        finally:
            connection.close()
        self.assertGreater(counted, 0)

    # -- fixtures ---------------------------------------------------------

    def _project(self) -> Path:
        _write_docx(
            self.root / "docs/活动系统.docx",
            [
                ("Heading1", "活动系统"),
                ("Heading2", "幸运转盘"),
                (None, "幸运转盘每天开启一次。"),
                (None, "玩家每日可参与5次。"),
                (None, "冷却时间30秒，活动时间20:00。"),
            ],
        )
        catalog_path = self.root / "knowledge" / "catalog.json"
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        catalog_path.write_text(json.dumps(CATALOG, ensure_ascii=False), encoding="utf-8")
        index_directory = self.root / ".index" / "knowledge"
        build_index_atomically(self.root, index_directory)
        return index_directory

    def _facts(self, index_directory: Path) -> SharedIndexRead:
        return SharedIndexRead(index_directory / "knowledge.sqlite")


class SemanticRetrievalTests(unittest.TestCase):
    """The vector channel as retrieval sees it: candidates, never answers."""

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="gdk-semantic-r-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "project"

    def test_a_vector_hit_is_read_back_and_stays_a_semantic_candidate(self) -> None:
        index_directory = self._project()
        provider = HashingTextEmbedding(256)
        channel = self._channel(index_directory, provider, threshold=0.05)

        with self._facts(index_directory) as index:
            response = retrieve(
                index, query="每日能参与几次", mode="hybrid", semantic=channel
            )

        self.assertEqual(response["mode"], {"requested": "hybrid", "effective": "hybrid"})
        self.assertEqual(response["response_meta"]["degradation_events"], [])
        self.assertFalse(response["response_meta"]["rebuild_vector_index_recommended"])
        self.assertEqual(response["status"], "found")
        self.assertEqual(
            response["response_meta"]["channels"][SEMANTIC_CHANNEL], "matched"
        )
        item = response["evidence"][0]
        self.assertEqual(item["match_type"], SEMANTIC_CANDIDATE)
        self.assertEqual(item["text"], "玩家每日可参与5次。")
        self.assertTrue(item["locator"])
        self.assertTrue(item["v2"]["source_reference"]["source_sha256"])
        channel_entry = next(
            entry
            for entry in item["channels"]
            if entry["channel"] == SEMANTIC_CHANNEL
        )
        self.assertIn("similarity", channel_entry)
        self.assertEqual(
            response["retrieval"]["semantic"]["model"]["model_id"],
            "local-hashing-text",
        )
        self.assertTrue(response["retrieval"]["semantic"]["queried"])

    def test_an_exact_or_alias_hit_is_never_displaced_and_scores_never_add(
        self,
    ) -> None:
        index_directory = self._project()
        channel = self._channel(
            index_directory, HashingTextEmbedding(256), threshold=0.0
        )

        with self._facts(index_directory) as index:
            response = retrieve(
                index, query="幸运转盘", mode="hybrid", semantic=channel
            )

        self.assertIn(
            response["candidates"][0]["match_type"],
            {"exact", "confirmed_alias", "lexical"},
        )
        self.assertEqual(
            response["candidates"][0]["unit_id"],
            response["evidence"][0]["unit_id"],
        )
        combined = [
            item
            for item in response["evidence"]
            if len(item["channels"]) > 1
        ]
        self.assertTrue(combined, "the same unit should be found twice")
        for item in combined:
            self.assertEqual(
                item["score"],
                min(
                    float(entry["raw_score"])
                    for entry in item["channels"]
                    if entry["raw_score"] is not None
                ),
            )
        found_by_vector = [
            item
            for item in response["candidates"]
            if any(
                entry["channel"] == SEMANTIC_CHANNEL for entry in item["channels"]
            )
        ]
        self.assertTrue(found_by_vector, response["candidates"])
        # One unit stays one candidate, and the stronger match type wins even
        # though the vector channel also found it.
        for item in found_by_vector:
            self.assertNotEqual(item["match_type"], SEMANTIC_CANDIDATE)
            entry = next(
                entry
                for entry in item["channels"]
                if entry["channel"] == SEMANTIC_CHANNEL
            )
            self.assertIn("similarity", entry)
        first_semantic = next(
            (
                position
                for position, item in enumerate(response["candidates"])
                if item["match_type"] == SEMANTIC_CANDIDATE
            ),
            len(response["candidates"]),
        )
        for item in response["candidates"][:first_semantic]:
            self.assertNotEqual(item["match_type"], SEMANTIC_CANDIDATE)

    def test_a_weak_vector_hit_is_possibly_related_and_never_an_answer(self) -> None:
        index_directory = self._project()
        channel = self._channel(
            index_directory, HashingTextEmbedding(256), threshold=0.99
        )

        with self._facts(index_directory) as index:
            response = retrieve(
                index, query="每日能参与几次", mode="hybrid", semantic=channel
            )

        self.assertEqual(response["status"], "not_found")
        self.assertEqual(response["evidence"], [])
        self.assertTrue(response["possible_related"])
        for item in response["possible_related"]:
            self.assertFalse(item["supports_project_fact"])
            self.assertEqual(item["boundary"], WEAK_SEMANTIC_BOUNDARY)
            self.assertEqual(item["match_type"], SEMANTIC_CANDIDATE)
        self.assertTrue(
            any("可能相关内容" in note for note in response["limitations"]),
            response["limitations"],
        )

    def test_a_semantic_request_runs_the_vector_channel_alone(self) -> None:
        index_directory = self._project()
        channel = self._channel(
            index_directory, HashingTextEmbedding(256), threshold=0.05
        )

        with self._facts(index_directory) as index:
            response = retrieve(
                index, query="每日能参与几次", mode="semantic", semantic=channel
            )

        self.assertEqual(
            response["mode"], {"requested": "semantic", "effective": "semantic"}
        )
        for report in response["channels"]:
            if report["channel"] in {"exact", "lexical", "confirmed_alias", "structures"}:
                self.assertEqual(report["status"], "skipped")
                self.assertEqual(report["reason"], "semantic_mode_only")
        self.assertTrue(response["evidence"])
        self.assertEqual(
            {item["match_type"] for item in response["evidence"]},
            {SEMANTIC_CANDIDATE},
        )
        self.assertTrue(
            SEMANTIC_MODE_BOUNDARY in response["limitations"],
            response["limitations"],
        )

    def test_auto_reaches_for_vectors_only_when_word_level_found_nothing(self) -> None:
        index_directory = self._project()
        channel = self._channel(
            index_directory, HashingTextEmbedding(256), threshold=0.05
        )

        with self._facts(index_directory) as index:
            hit = retrieve(
                index, query="玩家每日可参与5次。", mode="auto", semantic=channel
            )
            miss = retrieve(
                index, query="每日能参与几次", mode="auto", semantic=channel
            )

        self.assertEqual(hit["mode"]["effective"], "auto")
        self.assertEqual(hit["response_meta"]["degradation_events"], [])
        self.assertEqual(
            hit["response_meta"]["channels"][SEMANTIC_CHANNEL], "skipped"
        )
        self.assertEqual(
            miss["response_meta"]["channels"][SEMANTIC_CHANNEL], "matched"
        )
        self.assertEqual(miss["status"], "found")
        self.assertEqual(miss["evidence"][0]["match_type"], SEMANTIC_CANDIDATE)

    def test_a_vector_channel_that_fails_degrades_instead_of_answering(self) -> None:
        index_directory = self._project()

        class Broken:
            def availability(self, facts: object) -> dict[str, object]:
                return {
                    "available": True,
                    "status": STATUS_READY,
                    "reason": "",
                    "detail": "",
                    "identity": {
                        "model_id": "broken",
                        "model_version": "1",
                        "dimension": 8,
                    },
                    "index_version": SEMANTIC_SCHEMA_VERSION,
                    "vectors": 3,
                    "threshold": 0.2,
                    "namespaces": ["source_facts"],
                    "stale_vectors": 0,
                    "image_embedding": False,
                }

            def search(self, facts: object, **kwargs: object) -> dict[str, object]:
                raise RuntimeError("模型在查询时崩了")

        with self._facts(index_directory) as index:
            response = retrieve(
                index,
                query="玩家每日可参与5次。",
                mode="hybrid",
                semantic=Broken(),
            )

        self.assertEqual(response["status"], "degraded")
        self.assertEqual(
            response["response_meta"]["channels"][SEMANTIC_CHANNEL], "unavailable"
        )
        self.assertTrue(response["response_meta"]["rebuild_vector_index_recommended"])
        # The deterministic channels still answered the question.
        self.assertTrue(response["evidence"])

    def test_a_generated_variant_is_searched_and_never_touches_the_dictionary(
        self,
    ) -> None:
        index_directory = self._project()

        class Rewriter:
            name = "test-rewriter"
            version = "1"

            def variants(self, query: str) -> list[str]:
                return ["幸运转盘"]

        catalog_path = self.root / "knowledge" / "catalog.json"
        before = catalog_path.read_bytes()
        state_path = self.root / ".design-state" / "notation.json"
        state_before = state_path.read_bytes() if state_path.exists() else None

        with self._facts(index_directory) as index:
            response = retrieve(
                index,
                query="抽奖轮盘",
                mode="auto",
                rewriter=Rewriter(),
            )

        self.assertEqual(response["status"], "found")
        self.assertEqual(
            response["response_meta"]["channels"][GENERATED_CHANNEL], "matched"
        )
        self.assertEqual(response["evidence"][0]["match_type"], "lexical")
        variant_entry = next(
            entry
            for entry in response["evidence"][0]["channels"]
            if entry["channel"] == GENERATED_CHANNEL
        )
        self.assertIn("幸运转盘", variant_entry["reason"])
        self.assertEqual(
            response["response_meta"]["query_rewrite"]["applied"], 1
        )
        self.assertFalse(
            response["response_meta"]["query_rewrite"]["updates_notation_dictionary"]
        )
        self.assertEqual(catalog_path.read_bytes(), before)
        self.assertEqual(
            state_path.read_bytes() if state_path.exists() else None, state_before
        )

    def test_the_default_response_still_carries_the_new_optional_fields(self) -> None:
        index_directory = self._project()
        with self._facts(index_directory) as index:
            response = retrieve(index, query="玩家每日可参与5次。")

        self.assertIsNone(response["retrieval"]["semantic"])
        self.assertIsNone(response["response_meta"]["semantic"])
        self.assertEqual(response["possible_related"], [])
        self.assertEqual(
            response["retrieval"]["query_variants"]["status"], "not_configured"
        )
        self.assertEqual(
            response["retrieval"]["query_variants"]["reason"],
            rewrite.REWRITER_NOT_CONFIGURED,
        )
        self.assertEqual(
            response["response_meta"]["channels"][GENERATED_CHANNEL],
            "not_configured",
        )
        self.assertEqual(response["mode"], {"requested": "auto", "effective": "auto"})

    # -- fixtures ---------------------------------------------------------

    def _project(self) -> Path:
        _write_docx(
            self.root / "docs/活动系统.docx",
            [
                ("Heading1", "活动系统"),
                ("Heading2", "幸运转盘"),
                (None, "幸运转盘每天开启一次。"),
                (None, "玩家每日可参与5次。"),
            ],
        )
        catalog_path = self.root / "knowledge" / "catalog.json"
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        catalog_path.write_text(
            json.dumps(CATALOG, ensure_ascii=False), encoding="utf-8"
        )
        index_directory = self.root / ".index" / "knowledge"
        build_index_atomically(self.root, index_directory)
        return index_directory

    def _channel(
        self, index_directory: Path, provider: object, *, threshold: float
    ) -> SemanticChannel:
        sidecar = SemanticIndex(
            index_directory / SEMANTIC_INDEX_FILENAME, threshold=threshold
        )
        if not sidecar.exists():
            with self._facts(index_directory) as facts:
                sidecar.build(facts, provider)
        return SemanticChannel(provider, sidecar)

    def _facts(self, index_directory: Path) -> SharedIndexRead:
        return SharedIndexRead(index_directory / "knowledge.sqlite")


def _write_docx(path: Path, blocks: list[tuple[str | None, str]]) -> None:
    """A DOCX whose paragraph styles decide the section path of each block."""

    body = []
    for style, text in blocks:
        style_xml = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        body.append(f"<w:p>{style_xml}<w:r><w:t>{text}</w:t></w:r></w:p>")
    document_xml = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>{''.join(body)}</w:body>
</w:document>
"""
    relationships_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"/>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("word/document.xml", document_xml)
        archive.writestr("word/_rels/document.xml.rels", relationships_xml)


if __name__ == "__main__":
    unittest.main()
