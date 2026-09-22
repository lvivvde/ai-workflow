"""V2-07 through MCP: the review tools as an AI client sees them.

The client cannot reach the dictionary except through the two halves of a review
action, so what is checked here is the surface itself: the tools exist, a preview
call writes nothing, applying without the previewed token fails, and the answer
to "what does this token mean" carries a project fact only once a person has
confirmed one.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import tempfile
import unittest

from mcp import Client

try:
    from tests.notation_fixtures import (
        DOCUMENT,
        MIXED_ARROW_REGION,
        NEXT_STEP,
        build_project,
    )
except ModuleNotFoundError:  # pragma: no cover - discover imports tests as modules
    from notation_fixtures import (
        DOCUMENT,
        MIXED_ARROW_REGION,
        NEXT_STEP,
        build_project,
    )


REVIEW_TOOLS = (
    "notation_dictionary",
    "resolve_notation",
    "plan_review_action",
    "apply_review_action",
    "review_history",
)


class ReviewMcpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory(prefix="gdk-review-mcp-")
        self.addCleanup(self._temporary.cleanup)
        self.root = Path(self._temporary.name)
        self.index_directory = build_project(self.root)
        self._saved_environment = {
            name: os.environ.get(name)
            for name in (
                "GAME_DESIGN_INDEX_DIR",
                "GAME_DESIGN_PROJECT_ROOT",
                "GAME_DESIGN_STATE_DIR",
            )
        }
        os.environ["GAME_DESIGN_INDEX_DIR"] = str(self.index_directory)
        os.environ["GAME_DESIGN_PROJECT_ROOT"] = str(self.root)
        self.addCleanup(self._restore_environment)

    def _restore_environment(self) -> None:
        for name, value in self._saved_environment.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    def _call(self, tool: str, arguments: dict[str, object]) -> dict[str, object]:
        return asyncio.run(self._call_async(tool, arguments))

    @staticmethod
    async def _call_async(
        tool: str, arguments: dict[str, object]
    ) -> dict[str, object]:
        from game_design_knowledge.server import mcp

        async with Client(mcp) as client:
            result = await client.call_tool(tool, arguments)
        if result.is_error:
            raise AssertionError(result.content[0].text)
        if result.structured_content is not None:
            return result.structured_content
        raise AssertionError("the review tools are expected to answer with JSON")

    @staticmethod
    async def _failure_async(tool: str, arguments: dict[str, object]) -> str:
        from game_design_knowledge.server import mcp

        async with Client(mcp) as client:
            result = await client.call_tool(tool, arguments)
        if not result.is_error:
            raise AssertionError("expected the call to be refused")
        return result.content[0].text

    def test_the_review_tools_are_exposed(self) -> None:
        async def names() -> set[str]:
            from game_design_knowledge.server import mcp

            async with Client(mcp) as client:
                listed = await client.list_tools()
            return {tool.name for tool in listed.tools}

        exposed = asyncio.run(names())
        for tool in REVIEW_TOOLS:
            self.assertIn(tool, exposed)

    def test_a_confirm_needs_the_preview_token_and_the_confirmation_flag(self) -> None:
        intent: dict[str, object] = {
            "action": "confirm",
            "notation_token": NEXT_STEP,
            "meaning": "advance to the next state",
            "document": DOCUMENT,
            "region": MIXED_ARROW_REGION,
            "reason": "the legend printed beside this picture",
            "actor": "alice",
        }
        preview = self._call("plan_review_action", intent)
        self.assertEqual(preview["status"], "confirmation_required")
        self.assertEqual(
            self._call("notation_dictionary", {})["dictionary"]["entries"], []
        )

        unconfirmed = self._call(
            "apply_review_action", {**intent, "plan_token": preview["plan_token"]}
        )
        self.assertEqual(unconfirmed["status"], "confirmation_required")
        self.assertEqual(
            self._call("notation_dictionary", {})["dictionary"]["entries"], [],
            "an unconfirmed call must not write",
        )

        applied = self._call(
            "apply_review_action",
            {
                **intent,
                "plan_token": preview["plan_token"],
                "confirmed": True,
            },
        )
        self.assertEqual(applied["status"], "applied")
        self.assertEqual(applied["entry"]["meaning"], "advance to the next state")
        self.assertFalse(applied["derived_knowledge_index_modified"])

        dictionary = self._call("notation_dictionary", {})
        self.assertEqual(len(dictionary["dictionary"]["entries"]), 1)
        self.assertEqual(dictionary["dictionary"]["counts"]["confirmed"], 1)
        self.assertFalse(dictionary["supports_project_fact"]["candidates"])
        self.assertTrue(dictionary["candidates"])

        answered = self._call(
            "resolve_notation",
            {
                "notation_token": NEXT_STEP,
                "document": DOCUMENT,
                "region": MIXED_ARROW_REGION,
            },
        )
        self.assertEqual(answered["status"], "resolved")
        self.assertTrue(answered["supports_project_fact"])
        self.assertEqual(answered["resolved"]["meaning"], "advance to the next state")

        unknown = self._call(
            "resolve_notation",
            {
                "notation_token": NEXT_STEP,
                "document": DOCUMENT,
                "region": "region-9",
            },
        )
        self.assertIsNone(unknown["resolved"])
        self.assertFalse(unknown["supports_project_fact"])

        history = self._call("review_history", {})
        self.assertEqual([event["action"] for event in history["events"]], ["confirm"])
        self.assertEqual(history["events"][0]["payload"]["actor"], "alice")

    def test_a_token_that_no_longer_matches_is_refused_through_mcp(self) -> None:
        intent: dict[str, object] = {
            "action": "confirm",
            "notation_token": NEXT_STEP,
            "meaning": "advance to the next state",
            "document": DOCUMENT,
            "region": MIXED_ARROW_REGION,
            "reason": "the legend printed beside this picture",
            "actor": "alice",
        }
        preview = self._call("plan_review_action", intent)
        detail = asyncio.run(
            self._failure_async(
                "apply_review_action",
                {
                    **intent,
                    "plan_token": "0" * 64,
                    "confirmed": True,
                },
            )
        )
        self.assertIn("plan", detail.lower())
        self.assertEqual(
            self._call("notation_dictionary", {})["dictionary"]["entries"], []
        )
        self.assertTrue(preview["plan_token"])

    def test_a_candidate_can_be_rejected_without_touching_the_dictionary(self) -> None:
        candidates = self._call("notation_dictionary", {})["candidates"]
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["region"], MIXED_ARROW_REGION)
        self.assertFalse(candidate["supports_project_fact"])

        intent: dict[str, object] = {
            "action": "reject",
            "notation_token": NEXT_STEP,
            "document": DOCUMENT,
            "region": MIXED_ARROW_REGION,
            "candidate_id": candidate["candidate_id"],
            "reason": "the arrow block is a layout artefact, not notation",
            "actor": "alice",
        }
        preview = self._call("plan_review_action", intent)
        self.assertEqual(preview["writes"], ["journal/review_events.jsonl"])
        applied = self._call(
            "apply_review_action",
            {
                **intent,
                "plan_token": preview["plan_token"],
                "confirmed": True,
            },
        )
        self.assertEqual(applied["status"], "applied")
        self.assertIsNone(applied["entry"])

        view = self._call("notation_dictionary", {})
        self.assertEqual(view["dictionary"]["entries"], [])
        self.assertTrue(view["candidates"][0]["rejected"])
        answered = self._call(
            "resolve_notation",
            {
                "notation_token": NEXT_STEP,
                "document": DOCUMENT,
                "region": MIXED_ARROW_REGION,
            },
        )
        self.assertEqual(answered["status"], "not_found")
        self.assertIsNone(answered["resolved"])
        self.assertFalse(answered["supports_project_fact"])


if __name__ == "__main__":  # pragma: no cover - manual runs
    unittest.main()
