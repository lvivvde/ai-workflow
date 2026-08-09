from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil

from mcp import ClientSession, StdioServerParameters, stdio_client


async def _run(index_directory: Path) -> dict[str, object]:
    executable = shutil.which("game-design-knowledge-mcp")
    if executable is None:
        raise RuntimeError("game-design-knowledge-mcp is not installed on PATH")
    environment = os.environ.copy()
    environment["GAME_DESIGN_INDEX_DIR"] = os.fspath(index_directory.resolve())
    parameters = StdioServerParameters(command=executable, env=environment)
    async with stdio_client(parameters) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool("index_status", {})

    payload = result.model_dump(by_alias=True, exclude_none=True)
    if payload.get("isError"):
        raise RuntimeError(f"index_status returned an MCP error: {payload}")
    structured = payload.get("structuredContent")
    if structured is None:
        content = payload.get("content", [])
        if not content or "text" not in content[0]:
            raise RuntimeError(f"index_status returned no structured content: {payload}")
        structured = json.loads(content[0]["text"])
    if structured.get("schema_version") != 2:
        raise RuntimeError(f"Unexpected schema version: {structured}")
    if structured.get("is_stale") is not False:
        raise RuntimeError(f"Smoke-test index is stale: {structured}")
    return structured


def main() -> int:
    parser = argparse.ArgumentParser(description="Start the installed stdio MCP server and call index_status.")
    parser.add_argument("index_directory", type=Path)
    arguments = parser.parse_args()
    if not (arguments.index_directory / "knowledge.sqlite").is_file():
        parser.error("index_directory must contain knowledge.sqlite")
    result = asyncio.run(_run(arguments.index_directory))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
