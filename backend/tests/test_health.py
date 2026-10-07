from fastapi.testclient import TestClient

from app.main import create_app


def test_health() -> None:
    assert TestClient(create_app("sqlite://")).get("/api/health").json() == {"status": "ok"}
