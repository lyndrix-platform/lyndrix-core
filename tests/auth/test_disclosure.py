"""AZ-10 — what unauthenticated and plain ``api:read`` callers can learn.

The API map (OpenAPI/Swagger/ReDoc) was public, every viewer could read the
full configuration snapshot (database/Vault hosts, bootstrap account names,
directory mappings) and the cluster topology, and ``PATCH /api/users/{u}``
told any account which usernames exist.
"""
import pytest

from config import settings

DOC_ROUTES = ["/openapi.json", "/docs", "/redoc"]


@pytest.fixture()
def viewer(mint_key, bearer):
    return bearer(mint_key("viewer"))


@pytest.fixture()
def admin(mint_key, bearer):
    return bearer(mint_key("admin"))


# ── API docs ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", DOC_ROUTES)
def test_api_docs_stay_open_in_dev(client, path):
    assert settings.ENV_TYPE == "dev"
    assert client.get(path).status_code == 200


@pytest.mark.parametrize("path", DOC_ROUTES)
def test_api_docs_need_admin_read_outside_dev(client, viewer, admin, monkeypatch, path):
    monkeypatch.setattr(settings, "ENV_TYPE", "prod")
    anonymous = client.get(path)
    assert anonymous.status_code == 401 and anonymous.headers["www-authenticate"] == "Bearer"
    assert client.get(path, headers=viewer).status_code == 403
    assert client.get(path, headers=admin).status_code == 200


def test_the_schema_is_still_complete_for_an_administrator(client, admin, monkeypatch):
    monkeypatch.setattr(settings, "ENV_TYPE", "prod")
    schema = client.get("/openapi.json", headers=admin).json()
    assert "/api/auth/login" in schema["paths"] and "BearerAuth" in schema["components"]["securitySchemes"]
    assert "/openapi.json" not in schema["paths"]          # the docs routes themselves stay unlisted


# ── cluster ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/api/cluster", "/api/cluster/preflight"])
def test_cluster_inspection_is_for_administrators(client, viewer, admin, path):
    assert client.get(path, headers=viewer).status_code == 403
    assert client.get(path, headers=admin).status_code == 200


def test_probes_stay_public(client):
    assert client.get("/api/live").status_code == 200
    assert client.get("/api/ready").status_code in (200, 503)


# ── configuration snapshot ──────────────────────────────────────────────────


def test_viewer_gets_only_what_a_shell_needs(client, viewer):
    body = client.get("/api/system/config", headers=viewer).json()
    config = body["config"]
    assert body["restricted"] is True
    assert config["APP_NAME"] == settings.APP_NAME           # the shell's title for every user
    assert config["env_locked"] == [] and config["editable_settings"] == []
    assert set(config) == {"APP_NAME", "APP_TITLE", "DEFAULT_LOCALE", "SUPPORTED_LOCALES",
                           "DEFAULT_THEME_ID", "THEME_ENGINE_ENABLED", "env_locked", "editable_settings"}
    for internal in ("DB_HOST", "VAULT_URL", "LYNDRIX_ADMIN_USER", "LYNDRIX_LDAP_ROLE_MAPPING",
                     "LYNDRIX_PLUGINS_DESIRED", "DATABASE_URL_SAFE", "CORS_ALLOWED_ORIGINS"):
        assert internal not in config


def test_administrator_keeps_the_full_snapshot(client, admin):
    body = client.get("/api/system/config", headers=admin).json()
    assert body["restricted"] is False
    assert {"DB_HOST", "VAULT_URL", "APP_NAME", "env_locked", "editable_settings"} <= set(body["config"])
    assert body["config"]["editable_settings"]               # the settings pages are driven by this
    assert body["config"]["DB_PASSWORD"] == "***"            # secrets stay redacted for everyone


# ── user enumeration ────────────────────────────────────────────────────────


def test_patch_does_not_reveal_which_usernames_exist(client, viewer, admin):
    existing = client.patch("/api/users/admin", headers=viewer, json={"full_name": "x"})
    missing = client.patch("/api/users/no-such-user", headers=viewer, json={"full_name": "x"})
    assert existing.status_code == missing.status_code == 403
    assert existing.json() == missing.json()
    # With admin rights the distinction is legitimate; own profile works as before.
    assert client.patch("/api/users/no-such-user", headers=admin, json={"full_name": "x"}).status_code == 404
    assert client.patch("/api/users/viewer", headers=viewer, json={"full_name": "Vera"}).status_code == 200
    assert client.patch("/api/users/VIEWER", headers=viewer, json={"full_name": "Vera"}).status_code in (200, 404)
