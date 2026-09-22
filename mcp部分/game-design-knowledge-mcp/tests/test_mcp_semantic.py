"""V2-10 at the tool surface: one explicit switch, and a visible off state.

The contract tests check the vector channel directly. These check what a caller
reaches: that nothing loads until the switch names a local provider, that the
sidecar can be built, inspected and dropped without touching the facts, and
that a switch that is broken degrades visibly instead of quietly answering as
if the vector channel had never been asked for.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from mcp import Client

from game_design_knowledge.embeddings import EMBEDDING_PROVIDER_ENV
from game_design_knowledge.index_build import build_index_atomically
from game_design_knowledge.query_rewrite import QUERY_REWRITER_ENV
from game_design_knowledge.retrieval import GENERATED_CHANNEL, SEMANTIC_CANDIDATE
from game_design_knowledge.semantic_index import SEMANTIC_INDEX_FILENAME
from game_design_knowledge.server import mcp

try:
    from tests.document_fixtures import write_docx
except ModuleNotFoundError:  # pragma: no cover - discover imports tests as modules
    from document_fixtures import write_docx


ENVIRONMENT = (
    "GAME_DESIGN_INDEX_DIR",
    "GAME_DESIGN_PROJECT_ROOT",
    EMBEDDING_PROVIDER_ENV,
    QUERY_REWRITER_ENV,
)


class SemanticMcpTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="gdk-mcp-semantic-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "project"
        self._saved_environment = {
            name: os.environ.get(name) for name in ENVIRONMENT
        }

    def tearDown(self) -> None:
        for name, value in self._saved_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    # -- fixtures ---------------------------------------------------------

    def _index_project(self) -> Path:
        write_docx(self.root / "docs" / "活动系统.docx", "幸运转盘每天开启一次。")
        write_docx(self.root / "docs" / "参与规则.docx", "玩家每日可参与5次。")
        catalog_path = self.root / "knowledge" / "catalog.json"
        catalog_path.parent.mkdir(parents=True, exist_ok=True)
        catalog_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "features": [
                        {
                            "key": "lucky-wheel",
                            "canonical_name": "幸运转盘",
                            "source": "knowledge/catalog.json",
                            "aliases": [],
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        index_directory = self.root / ".index" / "knowledge"
        build_index_atomically(self.root, index_directory)
        os.environ["GAME_DESIGN_INDEX_DIR"] = os.fspath(index_directory)
        os.environ["GAME_DESIGN_PROJECT_ROOT"] = os.fspath(self.root)
        return index_directory

    async def _call(
        self, name: str, arguments: dict[str, object] | None = None
    ) -> dict[str, object]:
        async with Client(mcp) as client:
            result = await client.call_tool(name, arguments or {})
        if getattr(result, "is_error", False):
            raise AssertionError(f"{name} failed: {result.content}")
        payload = result.structured_content or (result.content or [{}])[0]
        return payload  # type: ignore[return-value]

    # -- the switch -------------------------------------------------------

    async def test_the_tool_surface_is_listed_and_starts_switched_off(self) -> None:
        async with Client(mcp) as client:
            listing = await client.list_tools()

        names = {tool.name for tool in listing.tools}
        for tool in (
            "semantic_index_status",
            "rebuild_semantic_index",
            "drop_semantic_index",
        ):
            self.assertIn(tool, names)

        index_directory = self._index_project()
        os.environ.pop(EMBEDDING_PROVIDER_ENV, None)
        off = await self._call("semantic_index_status")
        self.assertEqual(off["status"], "unavailable")
        self.assertEqual(off["provider"]["reason"], "provider_not_configured")
        self.assertFalse(off["provider"]["configured"])
        self.assertFalse(off["rebuild_recommended"])
        self.assertFalse(off["image_embedding"])
        self.assertFalse((index_directory / SEMANTIC_INDEX_FILENAME).exists())

        os.environ[EMBEDDING_PROVIDER_ENV] = "hashing:64"
        on = await self._call("semantic_index_status")
        self.assertEqual(on["status"], "missing")
        self.assertTrue(on["provider"]["configured"])
        self.assertEqual(on["provider"]["model"]["model_id"], "local-hashing-text")
        self.assertEqual(on["provider"]["model"]["dimension"], 64)
        self.assertTrue(on["rebuild_recommended"])
        self.assertTrue(on["index_status"]["is_stale"] is False)

    async def test_a_hybrid_answer_goes_through_the_built_sidecar(self) -> None:
        index_directory = self._index_project()
        os.environ[EMBEDDING_PROVIDER_ENV] = "hashing:256"
        os.environ.pop(QUERY_REWRITER_ENV, None)

        preview = await self._call("rebuild_semantic_index", {"confirmed": False})
        self.assertEqual(preview["status"], "confirmation_required")
        self.assertFalse((index_directory / SEMANTIC_INDEX_FILENAME).exists())

        built = await self._call("rebuild_semantic_index", {"confirmed": True})
        self.assertEqual(built["status"], "built")
        self.assertGreater(built["vectors"], 0)
        self.assertTrue(built["facts_index_untouched"])
        self.assertEqual(built["index"]["status"], "ready")
        self.assertTrue((index_directory / SEMANTIC_INDEX_FILENAME).is_file())

        response = await self._call(
            "retrieve_evidence", {"query": "每日能参与几次", "mode": "hybrid"}
        )
        self.assertEqual(response["mode"]["effective"], "hybrid")
        self.assertEqual(response["response_meta"]["degradation_events"], [])
        self.assertTrue(response["retrieval"]["semantic"]["queried"])
        self.assertEqual(
            response["evidence"][0]["match_type"], SEMANTIC_CANDIDATE
        )
        self.assertEqual(response["evidence"][0]["text"], "玩家每日可参与5次。")
        self.assertTrue(response["evidence"][0]["locator"])
        self.assertIn("index_status", response)

        drop_preview = await self._call("drop_semantic_index", {"confirmed": False})
        self.assertEqual(drop_preview["status"], "confirmation_required")
        dropped = await self._call("drop_semantic_index", {"confirmed": True})
        self.assertEqual(dropped["status"], "dropped")
        self.assertTrue(dropped["core_tools_unaffected"])
        self.assertFalse(dropped["index_status"]["is_stale"])
        self.assertFalse((index_directory / SEMANTIC_INDEX_FILENAME).exists())

        # The V1 tool never depended on any of this, and still does not.
        v1 = await self._call("search_evidence", {"query": "玩家每日可参与5次。"})
        self.assertEqual(v1["match_type"], "exact")
        self.assertTrue(v1["evidence"])
        after_drop = await self._call(
            "retrieve_evidence", {"query": "每日能参与几次", "mode": "hybrid"}
        )
        self.assertEqual(after_drop["status"], "degraded")
        self.assertEqual(after_drop["mode"]["effective"], "auto")

    async def test_a_broken_provider_switch_degrades_without_breaking_v1(self) -> None:
        self._index_project()
        os.environ[EMBEDDING_PROVIDER_ENV] = "no_such_module:provider"

        response = await self._call(
            "retrieve_evidence", {"query": "玩家每日可参与5次。", "mode": "hybrid"}
        )

        self.assertEqual(response["status"], "degraded")
        self.assertEqual(response["mode"]["effective"], "auto")
        event = response["response_meta"]["degradation_events"][0]
        self.assertEqual(event["reason"], "provider_not_importable")
        self.assertEqual(
            response["retrieval"]["unavailable_capabilities"], ["semantic"]
        )
        self.assertTrue(response["evidence"])
        self.assertTrue(response["response_meta"]["rebuild_vector_index_recommended"])

        v1 = await self._call("search_evidence", {"query": "玩家每日可参与5次。"})
        self.assertEqual(v1["match_type"], "exact")
        self.assertTrue(v1["evidence"])

    async def test_a_changed_model_switch_is_reported_instead_of_mixed(self) -> None:
        self._index_project()
        os.environ[EMBEDDING_PROVIDER_ENV] = "hashing:64"
        await self._call("rebuild_semantic_index", {"confirmed": True})

        os.environ[EMBEDDING_PROVIDER_ENV] = "hashing:32"
        response = await self._call(
            "retrieve_evidence", {"query": "每日能参与几次", "mode": "hybrid"}
        )
        self.assertEqual(response["status"], "degraded")
        event = response["response_meta"]["degradation_events"][0]
        self.assertEqual(event["reason"], "semantic_model_changed")
        self.assertEqual(response["evidence"], [])

        status = await self._call("semantic_index_status")
        self.assertEqual(status["status"], "incompatible")
        self.assertTrue(status["rebuild_recommended"])

        rebuilt = await self._call("rebuild_semantic_index", {"confirmed": True})
        self.assertEqual(rebuilt["status"], "built")
        self.assertEqual(rebuilt["model"]["dimension"], 32)
        again = await self._call(
            "retrieve_evidence", {"query": "每日能参与几次", "mode": "hybrid"}
        )
        self.assertEqual(again["mode"]["effective"], "hybrid")
        self.assertEqual(again["evidence"][0]["text"], "玩家每日可参与5次。")

    async def test_a_broken_rewriter_switch_is_reported_not_ignored(self) -> None:
        self._index_project()
        os.environ.pop(EMBEDDING_PROVIDER_ENV, None)
        os.environ[QUERY_REWRITER_ENV] = "no_such_module:Rewriter"

        response = await self._call(
            "retrieve_evidence", {"query": "玩家每日可参与5次。", "mode": "auto"}
        )

        self.assertEqual(response["response_meta"]["channels"][GENERATED_CHANNEL], "failed")
        self.assertEqual(
            response["response_meta"]["query_rewrite"]["reason"],
            "query_rewriter_not_importable",
        )
        self.assertFalse(
            response["response_meta"]["query_rewrite"]["updates_notation_dictionary"]
        )
        self.assertEqual(response["status"], "degraded")
        # The word-level channels answered anyway.
        self.assertTrue(response["evidence"])

        status = await self._call("semantic_index_status")
        self.assertEqual(status["rewriter"]["reason"], "query_rewriter_not_importable")


if __name__ == "__main__":
    unittest.main()
