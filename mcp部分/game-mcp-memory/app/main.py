from __future__ import annotations

import asyncio
from typing import Any

from fastapi import FastAPI, HTTPException, Query

from app.config import get_settings
from app.memory_service import MemoryService
from app.models import (
    AddMemoryRequest,
    CommandResultRequest,
    HealthResponse,
    MemoryResponse,
    SearchMemoryRequest,
)


settings = get_settings()
memory_service = MemoryService(settings)

app = FastAPI(
    title="Game MCP Memory Sidecar",
    version="0.1.0",
    description="Local long-term memory for the simple game client MCP.",
)


def _safe_error(exc: Exception) -> HTTPException:
    message = str(exc)
    if "API" in message.upper() and "KEY" in message.upper():
        message = "Memory provider credentials are missing or invalid"
    return HTTPException(status_code=503, detail=message)


@app.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    return HealthResponse(
        status="ok",
        service="game-mcp-memory",
        api_key_configured=settings.api_key_configured,
    )


@app.get("/config/status")
async def config_status() -> dict[str, object]:
    return settings.public_summary()


@app.post("/memories", response_model=MemoryResponse)
async def add_memory(request: AddMemoryRequest) -> MemoryResponse:
    try:
        result = await asyncio.to_thread(memory_service.add, request)
        return MemoryResponse(result=result)
    except Exception as exc:
        raise _safe_error(exc) from exc


@app.post("/memories/search", response_model=MemoryResponse)
async def search_memories(request: SearchMemoryRequest) -> MemoryResponse:
    try:
        result = await asyncio.to_thread(memory_service.search, request)
        return MemoryResponse(result=result)
    except Exception as exc:
        raise _safe_error(exc) from exc


@app.get("/memories", response_model=MemoryResponse)
async def list_memories(
    user_id: str | None = Query(default=None, max_length=128),
) -> MemoryResponse:
    try:
        result = await asyncio.to_thread(memory_service.get_all, user_id)
        return MemoryResponse(result=result)
    except Exception as exc:
        raise _safe_error(exc) from exc


@app.delete("/memories/{memory_id}", response_model=MemoryResponse)
async def delete_memory(memory_id: str) -> MemoryResponse:
    try:
        result = await asyncio.to_thread(memory_service.delete, memory_id)
        return MemoryResponse(result=result)
    except Exception as exc:
        raise _safe_error(exc) from exc


@app.post("/command-results", response_model=MemoryResponse)
async def record_command_result(request: CommandResultRequest) -> MemoryResponse:
    try:
        result = await asyncio.to_thread(memory_service.record_command_result, request)
        return MemoryResponse(result=result)
    except Exception as exc:
        raise _safe_error(exc) from exc
