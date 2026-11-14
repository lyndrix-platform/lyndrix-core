"""AZ-14 — ``PATCH /api/auth/config``.

An ``admin:write`` holder could change ``ldap_url`` / ``oidc_issuer`` while the
masked secret stayed stored: the next login sent the LDAP bind password or the
OIDC client secret to the new host. Unknown keys were merged into the
``core/auth`` Vault secret as well.
"""
import logging

import pytest

from core.components.auth.logic import auth_service as auth_service_module
from core.components.auth.logic.auth_config import auth_config_service
from core.components.auth.logic.auth_service import auth_service

LDAP_ENV = ["LYNDRIX_LDAP_URL", "LYNDRIX_LDAP_BIND_PASSWORD", "LYNDRIX_OIDC_ISSUER", "LYNDRIX_OIDC_CLIENT_SECRET"]


@pytest.fixture()
def stored(monkeypatch):
    """The ``core/auth`` Vault secret, pre-filled with a working LDAP + OIDC setup."""
    for name in LDAP_ENV:
        monkeypatch.delenv(name, raising=False)
    store = {
        "ldap_url": "ldaps://dc.corp.invalid:636",
        "ldap_bind_password": "dummy-bind-password-not-real",
        "oidc_issuer": "https://idp.corp.invalid/application/o/lyndrix",
        "oidc_client_secret": "dummy-client-secret-not-real",
    }

    def save(updates):
        for key, value in updates.items():
            if value == "":
                store.pop(key, None)
            else:
                store[key] = value

    monkeypatch.setattr(auth_config_service, "load_vault_data", lambda: dict(store))
    monkeypatch.setattr(auth_config_service, "save_vault_data", save)
    monkeypatch.setattr(auth_service, "reinitialize_providers", lambda: None)
    return store


@pytest.fixture()
def admin(mint_key, bearer):
    return bearer(mint_key("admin"))


def _patch(client, admin, **updates):
    return client.patch("/api/auth/config", headers=admin, json={"updates": updates})


def test_unknown_keys_are_rejected(client, admin, stored):
    before = dict(stored)
    r = _patch(client, admin, ldap_url="ldaps://dc.corp.invalid:636", totally_unknown="x")
    assert r.status_code == 422 and "totally_unknown" in r.json()["detail"]
    assert stored == before


@pytest.mark.parametrize("endpoint, secret, attacker", [
    ("ldap_url", "ldap_bind_password", "ldap://attacker.invalid:389"),
    ("oidc_issuer", "oidc_client_secret", "https://attacker.invalid/o/x"),
])
def test_repointing_an_endpoint_needs_its_secret_again(client, admin, stored, endpoint, secret, attacker):
    before = dict(stored)
    r = client.patch("/api/auth/config", headers=admin, json={"updates": {endpoint: attacker}})
    assert r.status_code == 422 and secret in r.json()["detail"]
    blank = client.patch("/api/auth/config", headers=admin, json={"updates": {endpoint: attacker, secret: "  "}})
    assert blank.status_code == 422                        # the masked field left empty is not "re-entered"
    assert stored == before

    ok = client.patch("/api/auth/config", headers=admin,
                      json={"updates": {endpoint: attacker, secret: "dummy-new-secret-not-real"}})
    assert ok.status_code == 200 and stored[endpoint] == attacker


def test_the_shell_resending_unchanged_fields_is_not_a_change(client, admin, stored):
    """The settings page sends every field on save and leaves secrets blank."""
    r = _patch(client, admin,
               ldap_url="ldaps://dc.corp.invalid:636", ldap_bind_password="",
               oidc_issuer="https://idp.corp.invalid/application/o/lyndrix/", oidc_client_secret="",
               ldap_user_filter="(sAMAccountName={username})")
    assert r.status_code == 200 and r.json()["skipped"] == ["ldap_bind_password", "oidc_client_secret"]
    assert stored["ldap_user_filter"] == "(sAMAccountName={username})"
    assert stored["ldap_bind_password"] == "dummy-bind-password-not-real"      # kept


def test_clearing_an_endpoint_or_first_time_setup_needs_no_secret(client, admin, stored):
    assert _patch(client, admin, ldap_url="").status_code == 200 and "ldap_url" not in stored
    stored.pop("oidc_client_secret")                        # nothing stored that could leak
    assert _patch(client, admin, oidc_issuer="https://new-idp.corp.invalid/o/x").status_code == 200


def test_secret_from_the_environment_pins_the_endpoint(client, admin, stored, monkeypatch):
    stored.pop("ldap_bind_password")
    monkeypatch.setenv("LYNDRIX_LDAP_BIND_PASSWORD", "dummy-env-bind-password-not-real")
    r = _patch(client, admin, ldap_url="ldap://attacker.invalid:389")
    assert r.status_code == 422 and "LYNDRIX_LDAP_BIND_PASSWORD" in r.json()["detail"]
    assert stored["ldap_url"] == "ldaps://dc.corp.invalid:636"


def test_cleartext_ldap_is_reported_at_startup(caplog):
    with caplog.at_level(logging.WARNING):
        auth_service_module._warn_about_ldap_transport("ldaps://dc.corp.invalid:636", True)
    assert caplog.text == ""
    with caplog.at_level(logging.WARNING):
        auth_service_module._warn_about_ldap_transport("LDAP://dc.corp.invalid:389", True)
        auth_service_module._warn_about_ldap_transport("ldaps://dc.corp.invalid:636", False)
    assert "UNENCRYPTED" in caplog.text and "verification is OFF" in caplog.text
