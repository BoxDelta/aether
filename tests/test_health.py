from fastapi.testclient import TestClient

from core import __version__
from core.app import create_app
from core.config import Settings


def test_health_returns_ok_and_version() -> None:
    client = TestClient(create_app(Settings(_env_file=None)))

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": __version__}


def test_health_rejects_non_get() -> None:
    client = TestClient(create_app(Settings(_env_file=None)))

    response = client.post("/health")

    assert response.status_code == 405


def test_create_app_returns_independent_instances() -> None:
    settings = Settings(_env_file=None)
    assert create_app(settings) is not create_app(settings)
