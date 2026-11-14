"""AZ-07 — ``admin:write`` must not reach the superadmin/system tier.

An INT_ADMIN could mint an API key for a superadmin account, reset its
password, write the master ``system_api_key`` and change ANY ``Settings`` field
at runtime (e.g. ``CORS_ALLOWED_ORIGINS`` — also the OIDC redirect allowlist).
"""
import asyncio

import pytest

from config import settings
from core.api import security
from core.components.auth.logic.models import User, UserApiKey
from core.components.settings.logic.system_config_service import system_config_service


@pytest.fixture()
def admin(mint_key, bearer):
    return bearer(mint_key("admin"))            # INT_ADMIN, no system flag


@pytest.fixture()
def superadmin(mint_key, bearer):
    return bearer(mint_key("breakglass"))       # superadmin system flag


@pytest.fixture()
def vault_store(monkeypatch):
    """Stand-in for the ``core/settings`` Vault secret."""
    from core.components.vault.logic import kv_helper

    store: dict = {}

    def update_secret_dict(path, mutate):
        working = dict(store)
        result = mutate(working)
        store.clear()
        store.update(working if result is None else result)
        return dict(store)

    monkeypatch.setattr(kv_helper, "update_secret_dict", update_secret_dict)
    return store


# ── credentials of a superadmin account ─────────────────────────────────────


def test_admin_cannot_mint_a_key_for_a_superadmin(client, auth_db, admin, superadmin):
    r = client.post("/api/users/breakglass/api-keys", headers=admin, json={"label": "x", "scopes": []})
    assert r.status_code == 403 and "superadmin" in r.json()["detail"]
    with auth_db() as s:
        assert s.query(UserApiKey).filter(UserApiKey.username == "breakglass").count() == 1  # the fixture's

    own = client.post("/api/users/breakglass/api-keys", headers=superadmin, json={"label": "mine"})
    assert own.status_code == 201


def test_admin_cannot_reset_a_superadmins_password_or_email(client, auth_db, admin, passwords):
    with auth_db() as s:
        before = s.query(User).filter(User.username == "breakglass").one().hashed_password
    assert client.patch("/api/users/breakglass", headers=admin,
                        json={"password": "dummy-taken-over-not-real"}).status_code == 403
    assert client.patch("/api/users/breakglass", headers=admin,
                        json={"email": "mallory@corp.invalid"}).status_code == 403
    assert client.post("/api/users/breakglass/password", headers=admin, json={
        "current_password": passwords["breakglass"], "new_password": "dummy-taken-over-not-real",
    }).status_code == 403
    with auth_db() as s:
        row = s.query(User).filter(User.username == "breakglass").one()
        assert row.hashed_password == before and row.email is None
    # Harmless profile fields stay editable for an administrator.
    assert client.patch("/api/users/breakglass", headers=admin, json={"full_name": "Break Glass"}).status_code == 200


def test_admin_cannot_delete_a_superadmin_or_revoke_its_keys(client, auth_db, admin, superadmin):
    with auth_db() as s:
        key_id = s.query(UserApiKey).filter(UserApiKey.username == "breakglass").one().id
    assert client.delete(f"/api/users/breakglass/api-keys/{key_id}", headers=admin).status_code == 403
    assert client.delete("/api/users/breakglass", headers=admin).status_code == 403
    assert client.get("/api/auth/me", headers=superadmin).status_code == 200      # still there, key intact


def test_superadmin_and_ordinary_targets_are_unaffected(client, admin, superadmin, passwords):
    assert client.patch("/api/users/breakglass", headers=superadmin, json={
        "password": "dummy-rotated-pass-not-real", "current_password": passwords["breakglass"],
        "email": "bg@corp.invalid",
    }).status_code == 200
    # Ordinary accounts: admin:write keeps its full reach.
    assert client.patch("/api/users/viewer", headers=admin,
                        json={"password": "dummy-reset-pass-not-real"}).status_code == 200
    assert client.post("/api/users/viewer/api-keys", headers=admin, json={"label": "ci"}).status_code == 201
    assert client.delete("/api/users/viewer", headers=admin).status_code == 200


# ── runtime configuration ───────────────────────────────────────────────────


@pytest.mark.parametrize("updates", [
    {"CORS_ALLOWED_ORIGINS": ["https://attacker.invalid"]},
    {"VAULT_SKIP_VERIFY": True},
    {"STORAGE_DIR": "/tmp/elsewhere"},
    {"LYNDRIX_API_BASIC_AUTH": True},
    {"no_such_key": "x"},
])
def test_only_editable_settings_can_be_changed_at_runtime(client, admin, superadmin, updates):
    before = list(settings.CORS_ALLOWED_ORIGINS), settings.VAULT_SKIP_VERIFY, settings.STORAGE_DIR
    for headers in (admin, superadmin):          # not a matter of privilege: deployment config is env-only
        r = client.post("/api/system/config", headers=headers,
                        json={"updates": updates, "persist_in_vault": False, "apply_runtime": True})
        assert r.status_code == 422 and "Unsupported config keys" in r.json()["detail"]
    assert (list(settings.CORS_ALLOWED_ORIGINS), settings.VAULT_SKIP_VERIFY, settings.STORAGE_DIR) == before


def test_editable_settings_and_tokens_still_work(client, admin, vault_store, monkeypatch):
    monkeypatch.setattr(settings, "APP_TITLE", settings.APP_TITLE)       # restored after the test
    r = client.post("/api/system/config", headers=admin, json={
        "updates": {"APP_TITLE": "Lyndrix Test", "github_token": "dummy-token-not-real"}})
    assert r.status_code == 200 and settings.APP_TITLE == "Lyndrix Test"
    assert vault_store == {"APP_TITLE": "Lyndrix Test", "github_token": "dummy-token-not-real"}


def test_admin_cannot_write_the_system_api_key(client, admin, vault_store):
    r = client.post("/api/system/config", headers=admin,
                    json={"updates": {"system_api_key": "dummy-chosen-key-not-real"}})
    assert r.status_code == 403 and vault_store == {}


def test_superadmin_writes_the_system_api_key_and_the_cached_key_is_dropped(
        client, superadmin, vault_store, monkeypatch):
    monkeypatch.delenv("LYNDRIX_SYSTEM_API_KEY", raising=False)
    monkeypatch.setitem(security._key_cache, "value", "dummy-old-key-not-real")
    monkeypatch.setitem(security._key_cache, "expires", 9e18)
    r = client.post("/api/system/config", headers=superadmin,
                    json={"updates": {"system_api_key": "dummy-new-key-not-real"}})
    assert r.status_code == 200 and vault_store == {"system_api_key": "dummy-new-key-not-real"}
    assert security._key_cache["expires"] == 0.0 and security._key_cache["value"] is None
    assert "dummy-new-key-not-real" not in r.text                        # never echoed back


def test_env_provided_system_key_cannot_be_overridden(client, superadmin, vault_store, monkeypatch):
    monkeypatch.setenv("LYNDRIX_SYSTEM_API_KEY", "dummy-env-key-not-real")
    r = client.post("/api/system/config", headers=superadmin,
                    json={"updates": {"system_api_key": "dummy-new-key-not-real"}})
    assert r.status_code == 409 and r.json()["detail"]["locked_fields"] == ["system_api_key"]
    assert vault_store == {}


def test_service_refuses_the_system_key_without_an_explicit_privilege(vault_store):
    with pytest.raises(PermissionError):
        system_config_service.apply({"system_api_key": "dummy-key-not-real"})
    with pytest.raises(PermissionError):
        system_config_service.apply({}, remove_keys=["system_api_key"])
    with pytest.raises(ValueError):
        system_config_service.apply({"CORS_ALLOWED_ORIGINS": ["https://attacker.invalid"]})
    assert vault_store == {}

    vault_store["system_api_key"] = "dummy-key-not-real"
    system_config_service.apply({}, remove_keys=["system_api_key"], allow_system_api_key=True)
    assert vault_store == {}


def test_a_peer_rotating_the_key_drops_the_cache_here(app, monkeypatch):
    import main

    async def no_vault(payload=None):
        return None

    monkeypatch.setattr(main, "_hydrate_settings_from_vault", no_vault)
    monkeypatch.setitem(security._key_cache, "value", "dummy-old-key-not-real")
    monkeypatch.setitem(security._key_cache, "expires", 9e18)
    asyncio.run(main._on_settings_changed_remote({"_origin": "peer-1", "keys": ["APP_TITLE"]}))
    assert security._key_cache["value"] == "dummy-old-key-not-real"       # unrelated change: kept
    asyncio.run(main._on_settings_changed_remote({"_origin": "peer-1", "removed": ["system_api_key"]}))
    assert security._key_cache["value"] is None
