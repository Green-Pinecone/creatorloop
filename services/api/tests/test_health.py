import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app


def test_health_reports_integrations_without_values():
    r = TestClient(app).get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert set(body["integrations"]) >= {"tigergraph", "dodo", "viasocket", "ai"}
    assert all(isinstance(v, bool) for v in body["integrations"].values())


def test_require_names_missing_settings():
    s = Settings(_env_file=None, tg_host=None)
    with pytest.raises(RuntimeError, match="TG_HOST"):
        s.require("tg_host")
