"""V2-09: what retrieval picks, what it reads back, and what it keeps apart.

Every test here asks one of the questions the retrieval base exists to answer:
did the query find something a person can check, is the ordering allowed to
decide anything, does a disagreement stay visible, and does the answer admit
when a channel could not run at all. Nothing is checked against a hand-written
response; the index is built from real documents and read back through the
same code the MCP tool calls.
"""

from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zipfile

from game_design_knowledge import retrieval
from game_design_knowledge.explanation import EVIDENCE_STATUSES as EXPLANATION_STATUSES
from game_design_knowledge.index_build import build_index_atomically
from game_design_knowledge.indexer import index_documents
from game_design_knowledge.ocr_regions import BoundingBox, OcrRegion, RegionObservation
from game_design_knowledge.retrieval import (
    CHANNELS,
    EVIDENCE_STATUSES,
    RESPONSE_STATES,
    RETRIEVAL_MODES,
    RETRIEVAL_VERSION,
    RetrievalError,
    retrieve,
)
from game_design_knowledge.shared_index import SharedIndexRead

try:
    from tests.document_fixtures import (
        png_bytes,
        write_docx_with_image,
        write_xlsx_with_image,
    )
except ModuleNotFoundError:  # pragma: no cover - discover imports tests as modules
    from document_fixtures import (
        png_bytes,
        write_docx_with_image,
        write_xlsx_with_image,
    )


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

ROTATION_ENTRY = {
    "entry_id": "notation:1",
    "notation_token": "抽卡轮盘",
    "meaning": "幸运转盘",
    "scope": {"kind": "project", "value": "*"},
    "status": "confirmed",
    "authority": "project_dictionary",
    "confirmed_by": "测试策划",
    "confirmed_at": "2026-08-09",
}


class RetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="gdk-retrieval-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name) / "project"

    # -- fixtures ---------------------------------------------------------

    def _project(
        self,
        documents: dict[str, list[tuple[str | None, str]]],
        *,
        catalog: dict[str, object] | None = None,
    ) -> Path:
        """Index a project whose DOCX documents are written from ``documents``."""

        for name, blocks in documents.items():
            _write_docx(self.root / name, blocks)
        if catalog is not None:
            catalog_path = self.root / "knowledge" / "catalog.json"
            catalog_path.parent.mkdir(parents=True, exist_ok=True)
            catalog_path.write_text(
                json.dumps(catalog, ensure_ascii=False), encoding="utf-8"
            )
        index_directory = self.root / ".index" / "knowledge"
        build_index_atomically(self.root, index_directory)
        return index_directory

    def _activity_project(self) -> Path:
        return self._project(
            {
                "docs/活动系统.docx": [
                    ("Heading1", "活动系统"),
                    ("Heading2", "幸运转盘"),
                    (None, "玩家每日可参与5次。"),
                ]
            },
            catalog=CATALOG,
        )

    def _index(self, index_directory: Path) -> SharedIndexRead:
        return SharedIndexRead(index_directory / "knowledge.sqlite")

    # -- channels ---------------------------------------------------------

    def test_an_exact_hit_is_reported_as_exact_and_read_back_with_its_locator(
        self,
    ) -> None:
        index_directory = self._activity_project()
        with self._index(index_directory) as index:
            response = retrieve(index, query="玩家每日可参与5次。")

        self.assertEqual(response["status"], "found")
        self.assertEqual(response["schema_version"], RETRIEVAL_VERSION)
        evidence = response["evidence"][0]
        self.assertEqual(evidence["match_type"], "exact")
        self.assertEqual(evidence["evidence_type"], "paragraph")
        # Two headings come first in the fixture, so the body paragraph is 3.
        self.assertEqual(evidence["locator"], {"paragraph_index": 3})
        self.assertEqual(evidence["section_path"], ["活动系统", "幸运转盘"])
        self.assertTrue(evidence["supports_project_fact"])
        self.assertEqual(evidence["unit_id"], f"evidence:{evidence['evidence_id']}")
        self.assertEqual(
            evidence["v2"]["source_reference"]["path"], evidence["source_document"]
        )
        self.assertTrue(evidence["v2"]["source_reference"]["source_sha256"])
        exact = _channel(response, "exact")
        self.assertEqual(exact["status"], "matched")
        self.assertEqual(exact["hits"], 1)

    def test_a_short_chinese_query_falls_back_to_a_word_match(self) -> None:
        index_directory = self._activity_project()
        with self._index(index_directory) as index:
            response = retrieve(index, query="参与")

        self.assertEqual(response["status"], "found")
        self.assertEqual(
            {item["text"] for item in response["evidence"]},
            {"玩家每日可参与5次。"},
        )

    def test_lexical_hits_are_ordered_after_exact_hits(self) -> None:
        index_directory = self._project(
            {
                "docs/规则.docx": [
                    (None, "幸运转盘每日可参与5次"),
                    (None, "活动期间幸运转盘每日可参与5次，上限10次。"),
                ]
            }
        )
        with self._index(index_directory) as index:
            response = retrieve(index, query="幸运转盘每日可参与5次")

        self.assertEqual(response["status"], "found")
        self.assertEqual(
            [item["match_type"] for item in response["evidence"]],
            ["exact", "lexical"],
        )
        # One unit, one candidate, with every channel that found it kept.
        self.assertEqual(
            response["evidence"][1]["channels"][0]["channel"], "lexical"
        )

    def test_a_confirmed_alias_expands_and_never_rewrites_the_query(self) -> None:
        index_directory = self._activity_project()
        with self._index(index_directory) as index:
            response = retrieve(index, query="转盘")

        self.assertEqual(response["query"], "转盘")
        self.assertEqual(response["status"], "found")
        self.assertEqual(
            [rule["kind"] for rule in response["expansions"]], ["catalog_alias"]
        )
        self.assertEqual(response["expansions"][0]["expanded_name"], "幸运转盘")
        match_types = {item["match_type"] for item in response["evidence"]}
        self.assertEqual(match_types, {"confirmed_alias"})
        alias_channel = _channel(response, "confirmed_alias")
        self.assertEqual(alias_channel["status"], "matched")
        self.assertEqual(alias_channel["expansions"], 1)
        reasons = [
            entry["reason"]
            for item in response["evidence"]
            for entry in item["channels"]
            if entry["channel"] == "confirmed_alias"
        ]
        self.assertTrue(any("转盘" in reason for reason in reasons), reasons)

    def test_a_notation_rule_expands_and_carries_the_rule_that_triggered_it(
        self,
    ) -> None:
        index_directory = self._activity_project()
        with self._index(index_directory) as index:
            response = retrieve(
                index,
                query="抽卡轮盘",
                notation={"entries": [ROTATION_ENTRY]},
            )

        self.assertEqual(response["status"], "found")
        rule = response["expansions"][0]
        self.assertEqual(rule["kind"], "notation_entry")
        self.assertEqual(rule["entry_id"], "notation:1")
        self.assertEqual(rule["direction"], "token_to_meaning")
        self.assertEqual(rule["status"], "confirmed")
        entry = next(
            channel
            for item in response["evidence"]
            for channel in item["channels"]
            if channel.get("rule")
        )
        self.assertEqual(entry["rule"]["notation_token"], "抽卡轮盘")
        self.assertEqual(entry["rule"]["scope"], "project:*")

    def test_a_notation_rule_for_another_document_does_not_expand(self) -> None:
        index_directory = self._activity_project()
        scoped = {
            **ROTATION_ENTRY,
            "scope": {
                "kind": "document",
                "value": "docs/别的文档.docx",
                "document": "docs/别的文档.docx",
            },
        }
        with self._index(index_directory) as index:
            response = retrieve(index, query="抽卡轮盘", notation={"entries": [scoped]})

        self.assertEqual(response["status"], "not_found")
        self.assertEqual(response["expansions"], [])

    def test_the_number_of_expansions_is_bounded_and_reported_as_applied(self) -> None:
        index_directory = self._activity_project()
        many = {
            "entries": [
                {
                    **ROTATION_ENTRY,
                    "entry_id": f"notation:{index}",
                    "notation_token": f"轮盘记法{index}",
                    "meaning": "幸运转盘",
                }
                for index in range(retrieval.MAX_ALIAS_EXPANSIONS + 4)
            ]
        }
        with self._index(index_directory) as index:
            response = retrieve(index, query="幸运转盘", notation=many)

        self.assertEqual(len(response["expansions"]), retrieval.MAX_ALIAS_EXPANSIONS)
        # The query is still the original one, and the cap is the channel's too.
        self.assertEqual(response["query"], "幸运转盘")
        self.assertEqual(
            [rule["direction"] for rule in response["expansions"]],
            ["meaning_to_token"] * retrieval.MAX_ALIAS_EXPANSIONS,
        )
        self.assertEqual(
            _channel(response, "confirmed_alias")["expansions"],
            retrieval.MAX_ALIAS_EXPANSIONS,
        )

    def test_an_xlsx_config_cell_is_a_retrieval_unit_too(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        write_xlsx_with_image(
            self.root / "docs/数值配置.xlsx", "每日挑战次数上限", png_bytes()
        )
        index_directory = self.root / ".index" / "knowledge"
        build_index_atomically(self.root, index_directory)
        with self._index(index_directory) as index:
            response = retrieve(index, query="每日挑战次数上限")

        self.assertEqual(response["status"], "found")
        evidence = response["evidence"][0]
        self.assertEqual(evidence["document_type"], "xlsx")
        self.assertEqual(
            set(evidence["locator"]), {"sheet_name", "cell_reference"}
        )
        self.assertEqual(evidence["locator"]["cell_reference"], "C5")

    # -- conflicts --------------------------------------------------------

    def test_a_high_ranked_hit_does_not_hide_the_other_side_of_its_claim(
        self,
    ) -> None:
        index_directory = self._project(
            {
                "docs/活动系统.docx": [(None, "幸运转盘每日可参与5次。")],
                "docs/活动系统-旧版.docx": [(None, "幸运转盘每日可参与3次。")],
            }
        )
        with self._index(index_directory) as index:
            response = retrieve(index, query="每日可参与5次")

        self.assertEqual(response["status"], "ambiguous")
        self.assertEqual(len(response["conflict_groups"]), 1)
        group = response["conflict_groups"][0]
        self.assertEqual(group["type"], "conflict_group")
        self.assertEqual(group["dimensions"], ["value"])
        self.assertEqual(group["resolution_state"], "unresolved")
        self.assertIsNone(group["winner"])
        self.assertNotIn("preferred_evidence_id", json.dumps(group))
        self.assertEqual(len(group["sides"]), 2)
        self.assertEqual([side["value"] for side in group["sides"]], ["3次", "5次"])
        # One side came from the query, the other from the claim scan: the query
        # never matched the older document, and the group still shows it.
        self.assertEqual(
            {side["retrieved"] for side in group["sides"]}, {True, False}
        )
        for side in group["sides"]:
            self.assertTrue(side["locator"])
            self.assertTrue(side["source_reference"]["source_sha256"])
            self.assertTrue(side["display_locator"]["label"])
        scan = _channel(response, "conflict_scan")
        self.assertEqual(scan["status"], "matched")
        self.assertEqual(scan["hits"], 1)
        self.assertNotIn(
            group["sides"][0]["unit_id"],
            {item["unit_id"] for item in response["evidence"]},
        )

    def test_a_difference_in_the_unit_alone_is_one_conflict_dimension(self) -> None:
        index_directory = self._project(
            {
                "docs/规则.docx": [(None, "幸运转盘冷却30秒。")],
                "docs/规则-新版.docx": [(None, "幸运转盘冷却30分钟。")],
            }
        )
        with self._index(index_directory) as index:
            response = retrieve(index, query="幸运转盘冷却")

        self.assertEqual(response["status"], "ambiguous")
        group = response["conflict_groups"][0]
        self.assertEqual(group["dimensions"], ["unit"])
        self.assertEqual(
            {side["value"] for side in group["sides"]}, {"30秒", "30分钟"}
        )
        self.assertEqual({side["unit"] for side in group["sides"]}, {"秒", "分钟"})

    def test_a_version_or_a_time_difference_stays_visible(self) -> None:
        index_directory = self._project(
            {
                "docs/规则.docx": [(None, "幸运转盘规则版本 v1 冷却30秒。")],
                "docs/规则-新版.docx": [(None, "幸运转盘规则版本 v2 冷却30秒。")],
                "docs/重置.docx": [(None, "幸运转盘每日 12:00 重置。")],
                "docs/重置-新版.docx": [(None, "幸运转盘每日 20:00 重置。")],
            }
        )
        with self._index(index_directory) as index:
            version_response = retrieve(index, query="幸运转盘规则版本")
            time_response = retrieve(index, query="幸运转盘每日")

        version_group = version_response["conflict_groups"][0]
        self.assertIn("version", version_group["dimensions"])
        self.assertEqual(
            {side["version"] for side in version_group["sides"]}, {"v1", "v2"}
        )
        time_group = time_response["conflict_groups"][0]
        self.assertIn("time", time_group["dimensions"])
        self.assertEqual(
            {side["time"] for side in time_group["sides"]}, {"12:00", "20:00"}
        )

    def test_two_wordings_of_one_value_stay_a_candidate_not_a_conflict(self) -> None:
        index_directory = self._project(
            {
                "docs/规则.docx": [(None, "幸运转盘每日可参与5次")],
                "docs/规则-副本.docx": [(None, "幸运转盘，每日可参与5次。")],
            }
        )
        with self._index(index_directory) as index:
            response = retrieve(index, query="幸运转盘每日可参与")

        self.assertEqual(response["status"], "found")
        self.assertEqual(response["conflict_groups"], [])
        potential = response["conflicts"][0]
        self.assertEqual(potential["type"], "potential_conflict_candidate")
        self.assertEqual(potential["evidence_type"], "paragraph")
        self.assertEqual(len(potential["evidence"]), 2)
        self.assertEqual(
            {item["retrieved"] for item in potential["evidence"]}, {True, False}
        )

    def test_identical_sentences_corroborate_instead_of_conflicting(self) -> None:
        index_directory = self._project(
            {
                "docs/规则.docx": [(None, "幸运转盘每日可参与5次。")],
                "docs/规则-副本.docx": [(None, "幸运转盘每日可参与5次。")],
            }
        )
        with self._index(index_directory) as index:
            response = retrieve(index, query="每日可参与5次")

        self.assertEqual(response["status"], "found")
        self.assertEqual(response["conflicts"], [])
        self.assertEqual(response["conflict_groups"], [])

    # -- not found, exploration, degradation ------------------------------

    def test_no_qualified_candidate_returns_not_found_without_a_weak_answer(
        self,
    ) -> None:
        index_directory = self._activity_project()
        with self._index(index_directory) as index:
            response = retrieve(index, query="风车玩法怎么重置")

        self.assertEqual(response["status"], "not_found")
        self.assertEqual(response["evidence"], [])
        self.assertEqual(response["candidates"], [])
        self.assertTrue(response["limitations"])
        self.assertEqual(_channel(response, "semantic")["status"], "not_configured")

    def test_unconfirmed_candidates_need_exploration_and_never_become_facts(
        self,
    ) -> None:
        index_directory = self._unqualified_ocr_project()
        with self._index(index_directory) as index:
            default = retrieve(index, query="回合结束奖励规则")
            explored = retrieve(
                index, query="回合结束奖励规则", include_candidates=True
            )

        self.assertEqual(default["status"], "not_found")
        self.assertEqual(default["candidates"], [])
        self.assertEqual(default["retrieval"]["held_back_unconfirmed"], 1)
        self.assertEqual(_channel(default, "unconfirmed")["status"], "skipped")

        self.assertEqual(explored["candidates"][0]["namespace"], "unconfirmed_candidates")
        self.assertEqual(explored["candidates"][0]["evidence_status"], "candidate")
        self.assertFalse(explored["candidates"][0]["supports_project_fact"])
        # A candidate is never an answer: exploration adds candidates only.
        self.assertEqual(explored["evidence"], [])
        self.assertEqual(explored["status"], "not_found")
        self.assertTrue(
            any("探索模式" in note for note in explored["limitations"]),
            explored["limitations"],
        )
        self.assertEqual(
            explored["namespaces"]["unconfirmed_candidates"]["facts"], False
        )

    def test_a_missing_namespace_degrades_while_fts5_keeps_answering(self) -> None:
        index_directory = self._activity_project()
        _drop_ocr_and_layout_tables(index_directory / "knowledge.sqlite")
        with self._index(index_directory) as index:
            response = retrieve(index, query="玩家每日可参与5次。")

        self.assertEqual(response["status"], "degraded")
        self.assertEqual(
            [item["text"] for item in response["evidence"]],
            ["玩家每日可参与5次。"],
        )
        for name in ("exact", "lexical"):
            channel = _channel(response, name)
            self.assertEqual(channel["status"], "unavailable")
            self.assertEqual(channel["reason"], "namespace_not_in_index")
            self.assertIn("不可读", channel["detail"])
        self.assertEqual(_channel(response, "structures")["status"], "unavailable")
        self.assertEqual(
            response["response_meta"]["channels"]["semantic"], "not_configured"
        )

    def test_a_capability_the_caller_could_not_supply_is_reported_as_degradation(
        self,
    ) -> None:
        index_directory = self._activity_project()
        with self._index(index_directory) as index:
            response = retrieve(
                index,
                query="玩家每日可参与5次。",
                degradations=[
                    {
                        "channel": "notation_dictionary",
                        "reason": "capability_unavailable",
                        "detail": "确认记法字典不可用。",
                    }
                ],
            )

        self.assertEqual(response["status"], "degraded")
        self.assertEqual(response["retrieval"]["unavailable_capabilities"], ["notation_dictionary"])
        event = response["response_meta"]["degradation_events"][0]
        self.assertEqual(event["channel"], "notation_dictionary")
        self.assertEqual(event["requested_mode"], "auto")
        self.assertEqual(event["effective_mode"], "auto")
        self.assertTrue(
            any("notation_dictionary" in note for note in response["limitations"]),
            response["limitations"],
        )
        # A missing dictionary is nothing to rebuild, unlike a missing vector
        # index: the recommendation stays false.
        self.assertFalse(response["response_meta"]["rebuild_vector_index_recommended"])

    def test_the_vector_channel_is_reported_missing_honestly(self) -> None:
        index_directory = self._activity_project()
        with self._index(index_directory) as index:
            auto = retrieve(index, query="玩家每日可参与5次。", mode="auto")
            lexical = retrieve(index, query="玩家每日可参与5次。", mode="lexical")
            hybrid = retrieve(index, query="玩家每日可参与5次。", mode="hybrid")
            semantic = retrieve(index, query="玩家每日可参与5次。", mode="semantic")

        self.assertEqual(auto["mode"], {"requested": "auto", "effective": "auto"})
        self.assertEqual(auto["response_meta"]["degradation_events"], [])
        self.assertFalse(auto["response_meta"]["rebuild_vector_index_recommended"])
        self.assertEqual(lexical["mode"]["effective"], "lexical")
        self.assertEqual(
            lexical["response_meta"]["vector"]["status"], "not_requested"
        )

        for response in (hybrid, semantic):
            self.assertEqual(response["mode"]["effective"], "auto")
            self.assertEqual(response["status"], "degraded")
            self.assertTrue(response["response_meta"]["rebuild_vector_index_recommended"])
            event = response["response_meta"]["degradation_events"][0]
            self.assertEqual(event["reason"], "vector_capability_not_configured")
            self.assertEqual(event["effective_mode"], "auto")
            # The deterministic channels still answered.
            self.assertTrue(response["evidence"])

    # -- read-back --------------------------------------------------------

    def test_a_cached_hit_that_cannot_be_read_back_becomes_untraceable(self) -> None:
        index_directory = self._activity_project()
        with self._index(index_directory) as index:
            response = retrieve(index, query="玩家每日可参与5次。")
            self.assertEqual(response["untraceable"], [])
            evidence = response["evidence"][0]
            cached = {
                "unit_id": evidence["unit_id"],
                "namespace": evidence["namespace"],
                "match_type": evidence["match_type"],
                "unit_type": evidence["evidence_type"],
                "text": evidence["text"],
                "evidence_status": evidence["evidence_status"],
                "source_document": evidence["source_document"],
                "document_type": evidence["document_type"],
                "record_id": evidence["evidence_id"],
                "recorded_sha256": evidence["v2"]["source_reference"]["source_sha256"],
                "regions": {},
                "channels": evidence["channels"],
            }
            gone = {**cached, "record_id": 10**6}
            changed = {**cached, "recorded_sha256": "0" * 64}
            # The public response deliberately does not carry these cache
            # fields, so the read-back rule is checked where it is implemented.
            resolved, untraceable = retrieval._hydrate(
                index, [cached, gone, changed]
            )

        self.assertEqual([item["unit_id"] for item in resolved], [cached["unit_id"]])
        self.assertEqual(
            [(item["unit_id"], item["reason"]) for item in untraceable],
            [
                (gone["unit_id"], "unit_not_in_index"),
                (changed["unit_id"], "source_changed_since_hit"),
            ],
        )
        for item in untraceable:
            self.assertFalse(item["supports_project_fact"])
            self.assertEqual(item["boundary"], retrieval.UNTRACEABLE_BOUNDARY)

    def test_the_response_vocabulary_stays_inside_the_declared_one(self) -> None:
        index_directory = self._activity_project()
        with self._index(index_directory) as index:
            response = retrieve(index, query="幸运转盘")

        self.assertIn(response["status"], RESPONSE_STATES)
        self.assertEqual([item["channel"] for item in response["channels"]], list(CHANNELS))
        for channel in response["channels"]:
            self.assertIn(
                channel["status"],
                {"matched", "no_match", "not_configured", "unavailable", "skipped", "failed"},
            )
        self.assertEqual(set(response["namespaces"]), {
            *retrieval.NAMESPACES,
            "untraceable_candidates",
        })
        self.assertEqual(EVIDENCE_STATUSES, EXPLANATION_STATUSES)
        for item in response["evidence"]:
            self.assertIn(item["evidence_status"], EXPLANATION_STATUSES)

    def test_a_query_a_mode_and_a_limit_are_validated_before_any_lookup(self) -> None:
        index_directory = self._activity_project()
        with self._index(index_directory) as index:
            for query in ("", "   "):
                with self.assertRaises(RetrievalError):
                    retrieve(index, query=query)
            with self.assertRaises(RetrievalError):
                retrieve(index, query="幸运转盘", mode="fuzzy")
            with self.assertRaises(RetrievalError):
                retrieve(index, query="幸运转盘", limit=0)
            with self.assertRaises(RetrievalError):
                retrieve(index, query="幸运转盘", limit=retrieval.MAX_LIMIT + 1)

        self.assertEqual(
            set(RETRIEVAL_MODES), {"lexical", "auto", "hybrid", "semantic"}
        )

    # -- helpers ----------------------------------------------------------

    def _unqualified_ocr_project(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        write_docx_with_image(self.root / "docs/活动系统.docx", "战斗流程示意", png_bytes())
        index_directory = self.root / ".index" / "knowledge"
        index_documents(
            self.root,
            index_directory,
            ocr_engine="rapidocr",
            ocr_providers={"rapidocr": _unqualified_observation},
        )
        return index_directory


def _unqualified_observation(path: Path) -> RegionObservation:
    """A run that produced text the quality gate refused."""

    return RegionObservation(
        engine="rapidocr",
        engine_version="1.3.24",
        tier="core",
        provider_status="succeeded",
        regions=(
            OcrRegion(
                index=0,
                text="回合结束奖励规则",
                bbox=BoundingBox(100.0, 0.0, 80.0, 20.0),
                reading_order=0,
                text_confidence=0.2,
                region_confidence=0.3,
                language="zh",
            ),
        ),
        language="zh",
    )


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


def _drop_ocr_and_layout_tables(database_path: Path) -> None:
    """An index built before the OCR and layout layers existed."""

    connection = sqlite3.connect(database_path)
    try:
        for table in (
            "ocr_regions",
            "ocr_runs",
            "ocr_normalizations",
            "structural_relations",
            "layout_elements",
            "layout_runs",
        ):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.commit()
    finally:
        connection.close()


def _channel(response: dict[str, object], name: str) -> dict[str, object]:
    return next(
        item
        for item in response["channels"]  # type: ignore[union-attr]
        if item["channel"] == name
    )


if __name__ == "__main__":
    unittest.main()
