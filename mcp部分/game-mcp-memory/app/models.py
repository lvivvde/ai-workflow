from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AddMemoryRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8_000)
    user_id: str | None = Field(default=None, min_length=1, max_length=128)
    agent_id: str = Field(default="simple-game-client", min_length=1, max_length=128)
    run_id: str | None = Field(default=None, max_length=128)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SearchMemoryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=2_000)
    user_id: str | None = Field(default=None, min_length=1, max_length=128)
    agent_id: str = Field(default="simple-game-client", min_length=1, max_length=128)
    run_id: str | None = Field(default=None, max_length=128)
    filters: dict[str, Any] = Field(default_factory=dict)
    limit: int = Field(default=5, ge=1, le=20)


class CommandResultRequest(BaseModel):
    environment: str = Field(min_length=1, max_length=64)
    server_id: str = Field(min_length=1, max_length=128)
    server_version: str | None = Field(default=None, max_length=128)
    command: str = Field(min_length=1, max_length=256)
    arguments: dict[str, Any] = Field(default_factory=dict)
    success: bool
    error_code: str | int | None = None
    message: str | None = Field(default=None, max_length=4_000)
    user_id: str | None = Field(default=None, min_length=1, max_length=128)
    run_id: str | None = Field(default=None, max_length=128)


class MemoryResponse(BaseModel):
    result: Any


class HealthResponse(BaseModel):
    status: str
    service: str
    api_key_configured: bool
