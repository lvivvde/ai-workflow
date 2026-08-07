from pathlib import Path
from typing import Any

from pydantic import SecretStr

from app.config import Settings
from app.memory_service import MemoryService
from app.models import AddMemoryRequest, CommandResultRequest, SearchMemoryRequest


class FakeMemory:
    def __init__(self) -> None:
        self.add_calls: list[tuple[str, dict[str, Any]]] = []
        self.search_calls: list[dict[str, Any]] = []

    def add(self, text: str, **kwargs: Any) -> dict[str, Any]:
        self.add_calls.append((text, kwargs))
        return {"results": [{"id": "memory-1", "event": "ADD"}]}

    def search(self, **kwargs: Any) -> dict[str, Any]:
        self.search_calls.append(kwargs)
        return {"results": [{"id": "memory-1", "memory": "reason参数必填"}]}


def build_service(tmp_path: Path) -> tuple[MemoryService, FakeMemory]:
    fake = FakeMemory()
    settings = Settings(
        memory_api_key=SecretStr("test-only-key"),
        memory_data_dir=tmp_path,
    )
    service = MemoryService(settings, memory_factory=lambda _: fake)
    return service, fake


def test_add_sanitizes_metadata_and_text(tmp_path: Path) -> None:
    service, fake = build_service(tmp_path)

    service.add(
        AddMemoryRequest(
            text="调用使用 sk-abcdefghijklmnop",
            metadata={"token": "secret-token", "environment": "test"},
        )
    )

    text, kwargs = fake.add_calls[0]
    assert "sk-abcdefghijklmnop" not in text
    assert kwargs["metadata"]["token"] == "[REDACTED]"
    assert kwargs["metadata"]["environment"] == "test"


def test_search_applies_default_scope(tmp_path: Path) -> None:
    service, fake = build_service(tmp_path)

    service.search(SearchMemoryRequest(query="金币指令有什么限制？"))

    call = fake.search_calls[0]
    assert call["filters"]["user_id"] == "local-user"
    assert call["filters"]["agent_id"] == "simple-game-client"
    assert call["top_k"] == 5


def test_command_result_becomes_scoped_memory(tmp_path: Path) -> None:
    service, fake = build_service(tmp_path)

    service.record_command_result(
        CommandResultRequest(
            environment="test",
            server_id="game-101",
            server_version="1.8",
            command="add_currency",
            arguments={"player_id": "10086", "password": "do-not-store"},
            success=False,
            error_code=1042,
            message="reason is required",
        )
    )

    text, kwargs = fake.add_calls[0]
    assert "reason is required" in text
    assert "do-not-store" not in text
    assert kwargs["metadata"]["environment"] == "test"
    assert kwargs["metadata"]["server_version"] == "1.8"
