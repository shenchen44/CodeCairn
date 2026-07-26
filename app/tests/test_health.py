from app.core.config import get_settings


def test_health_returns_meta(client) -> None:
    settings = get_settings()
    response = client.get("/health")
    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "codecairn"
    assert isinstance(payload["version"], str)
    assert payload["environment"] == settings.app_env
    assert isinstance(payload["dashboard_enabled"], bool)
