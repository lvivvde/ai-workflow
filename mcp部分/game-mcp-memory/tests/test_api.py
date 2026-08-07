from fastapi.testclient import TestClient

from app.main import app


client = TestClient(app)


def test_health_does_not_require_provider_call() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["service"] == "game-mcp-memory"


def test_config_status_never_exposes_api_key() -> None:
    response = client.get("/config/status")

    assert response.status_code == 200
    body = response.json()
    assert "api_key" not in body
    assert "api_key_configured" in body
