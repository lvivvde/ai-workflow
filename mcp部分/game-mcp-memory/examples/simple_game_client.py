"""Minimal integration example for the future simple-game-client MCP."""

from __future__ import annotations

from typing import Any

import httpx


class OptionalMemoryClient:
    def __init__(self, base_url: str = "http://127.0.0.1:18765") -> None:
        self.base_url = base_url.rstrip("/")

    def search_before_command(
        self,
        query: str,
        *,
        environment: str,
        server_version: str,
    ) -> list[dict[str, Any]]:
        try:
            response = httpx.post(
                f"{self.base_url}/memories/search",
                json={
                    "query": query,
                    "filters": {
                        "environment": environment,
                        "server_version": server_version,
                    },
                    "limit": 5,
                },
                timeout=3.0,
            )
            response.raise_for_status()
            return response.json()["result"].get("results", [])
        except (httpx.HTTPError, KeyError, TypeError):
            # Memory is optional: game commands must continue without it.
            return []

    def record_after_command(self, result: dict[str, Any]) -> bool:
        try:
            response = httpx.post(
                f"{self.base_url}/command-results",
                json=result,
                timeout=8.0,
            )
            response.raise_for_status()
            return True
        except httpx.HTTPError:
            return False
