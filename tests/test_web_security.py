from __future__ import annotations

import base64

import pytest
from fastapi.testclient import TestClient

from multi_agent_cad.web import server
from multi_agent_cad.execution_security import sanitized_subprocess_env


def _basic_header(username: str, password: str) -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def test_health_is_public_and_has_security_headers(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_BASIC_AUTH_USER", "pilot")
    monkeypatch.setattr(server, "_BASIC_AUTH_PASSWORD", "secret")
    with TestClient(server.app) as client:
        response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


def test_basic_auth_protects_application(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_BASIC_AUTH_USER", "pilot")
    monkeypatch.setattr(server, "_BASIC_AUTH_PASSWORD", "secret")
    with TestClient(server.app) as client:
        denied = client.get("/api/config/schema")
        allowed = client.get(
            "/api/config/schema", headers=_basic_header("pilot", "secret")
        )
    assert denied.status_code == 401
    assert denied.headers["www-authenticate"] == 'Basic realm="FormForge"'
    assert allowed.status_code == 200


def test_server_destination_path_is_rejected() -> None:
    with TestClient(server.app) as client:
        response = client.post(
            "/api/run",
            json={"prompt": "make a plate", "dest_path": "/tmp/should-not-be-written"},
        )
    assert response.status_code == 400
    assert "destination paths" in response.json()["detail"]


def test_model_service_url_must_be_allowlisted() -> None:
    with pytest.raises(server.HTTPException) as exc_info:
        server._validated_web_config({"DS_BASE_URL": "http://169.254.169.254/latest"})
    assert exc_info.value.status_code == 400


def test_client_key_can_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_ALLOW_CLIENT_API_KEY", False)
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    with TestClient(server.app) as client:
        response = client.post(
            "/api/run",
            json={"prompt": "make a plate", "api_key": "must-not-be-used"},
        )
    assert response.status_code == 503
    assert response.json()["detail"] == "model service credential is not configured"


def test_client_key_mode_never_falls_back_to_owner_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "_ALLOW_CLIENT_API_KEY", True)
    monkeypatch.setenv("DASHSCOPE_API_KEY", "owner-key-must-not-be-used")
    with pytest.raises(server.HTTPException) as exc_info:
        server._resolve_api_key({})
    assert exc_info.value.status_code == 400


def test_sensitive_environment_is_removed_from_generated_process() -> None:
    source = {
        "PATH": "/usr/bin",
        "DASHSCOPE_API_KEY": "visitor-secret",
        "OPENAI_API_KEY": "visitor-secret",
        "VOLC_ACCESS_KEY_ID": "cloud-secret",
        "VOLC_SECRET_ACCESS_KEY": "cloud-secret",
        "ITERATION": "2",
    }
    clean = sanitized_subprocess_env(source)
    assert clean == {"PATH": "/usr/bin", "ITERATION": "2"}


def test_production_client_key_mode_needs_no_owner_key_or_basic_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(server, "_DEPLOYMENT_MODE", "production")
    monkeypatch.setattr(server, "_ALLOW_CLIENT_API_KEY", True)
    monkeypatch.setattr(server, "_TRUST_GATEWAY_AUTH", False)
    monkeypatch.setattr(server, "_BASIC_AUTH_USER", "")
    monkeypatch.setattr(server, "_BASIC_AUTH_PASSWORD", "")
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    server._validate_startup_configuration()


def test_production_requires_authentication(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server, "_DEPLOYMENT_MODE", "production")
    monkeypatch.setattr(server, "_ALLOW_CLIENT_API_KEY", False)
    monkeypatch.setattr(server, "_TRUST_GATEWAY_AUTH", False)
    monkeypatch.setattr(server, "_BASIC_AUTH_USER", "")
    monkeypatch.setattr(server, "_BASIC_AUTH_PASSWORD", "")
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-only")
    with pytest.raises(RuntimeError, match="requires Basic Auth"):
        server._validate_startup_configuration()
