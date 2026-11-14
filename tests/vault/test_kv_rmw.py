"""Vault read-modify-write safety (audit finding BG-02).

A KV v2 write replaces the whole secret. These tests pin the rule that no
writer may write after a FAILED read (that turned one transient Vault 5xx into
the loss of every other key), that writes are check-and-set, and that a CAS
conflict re-reads and re-applies.

Runs without Vault: ``vault_instance`` is replaced by an in-memory KV v2 fake.
"""
import asyncio
from types import SimpleNamespace

import hvac.exceptions
import pytest
from fastapi import HTTPException

import core.services  # noqa: F401  (import order: services before api)
from core.api import security
from core.components.plugins.api import plugins_api
from core.components.plugins.logic.context import ModuleContext
from core.components.plugins.logic.models import ModuleManifest, ModulePermissions, PluginSettingField
from core.components.settings.logic.system_config_service import system_config_service
from core.components.vault.logic import kv_helper

PID = "lyndrix.plugin.kvtest"


class FakeKv:
    """Minimal KV v2 with versions, CAS and injectable failures."""

    def __init__(self):
        self.store: dict[str, tuple[dict, int]] = {}
        self.fail_reads = 0          # next N reads raise a 5xx
        self.conflict_writes = 0     # next N writes are preceded by a "peer" write
        self.writes: list[tuple[str, dict, object]] = []

    # hvac surface -------------------------------------------------------
    def read_secret_version(self, path, mount_point="secret", raise_on_deleted_version=None):
        if self.fail_reads:
            self.fail_reads -= 1
            raise hvac.exceptions.InternalServerError("vault leader election in progress")
        if path not in self.store:
            raise hvac.exceptions.InvalidPath("no secret")
        data, version = self.store[path]
        return {"data": {"data": dict(data), "metadata": {"version": version}}}

    def read_secret_metadata(self, path, mount_point="secret"):
        if path not in self.store:
            raise hvac.exceptions.InvalidPath("no metadata")
        return {"data": {"current_version": self.store[path][1]}}

    def create_or_update_secret(self, path, secret, cas=None, mount_point="secret"):
        if self.conflict_writes:
            self.conflict_writes -= 1
            data, version = self.store.get(path, ({}, 0))
            self.store[path] = ({**data, "peer_key": "peer-value"}, version + 1)
        current = self.store.get(path, ({}, 0))[1]
        if cas is not None and cas != current:
            raise hvac.exceptions.InvalidRequest(
                "check-and-set parameter did not match the current version"
            )
        self.writes.append((path, dict(secret), cas))
        self.store[path] = (dict(secret), current + 1)


@pytest.fixture()
def kv(monkeypatch):
    fake = FakeKv()
    client = SimpleNamespace(secrets=SimpleNamespace(kv=SimpleNamespace(v2=fake)))
    monkeypatch.setattr(core.services.vault_instance, "client", client)
    monkeypatch.setattr(core.services.vault_instance, "is_connected", True)
    monkeypatch.setattr(kv_helper.time, "sleep", lambda _s: None)
    return fake


def _ctx() -> ModuleContext:
    return ModuleContext(ModuleManifest(
        id=PID, name="KV Tester", version="0.0.1", type="PLUGIN",
        permissions=ModulePermissions(emit=[], subscribe=[]),
    ))


# --- helper -------------------------------------------------------------------

def test_strict_read_tells_absent_from_error(kv):
    assert kv_helper.read_secret_strict("nope") == ({}, 0)
    kv.fail_reads = 1
    with pytest.raises(kv_helper.VaultReadError):
        kv_helper.read_secret_strict("nope")
    # The tolerant reader keeps its contract for pure readers.
    kv.fail_reads = 1
    assert kv_helper.read_secret_dict("nope") == {}


def test_update_uses_cas_and_retries_on_conflict(kv):
    kv.store["p"] = ({"a": "1"}, 3)
    kv.conflict_writes = 1

    out = kv_helper.update_secret_dict("p", lambda d: d.update(b="2"))

    # First attempt lost the CAS, the retry re-read the peer's write and merged.
    assert out == {"a": "1", "peer_key": "peer-value", "b": "2"}
    assert kv.writes == [("p", out, 4)]


def test_update_creates_with_cas_zero_and_skips_noop(kv):
    kv_helper.update_secret_dict("new", lambda d: d.update(a="1"))
    assert kv.writes == [("new", {"a": "1"}, 0)]
    kv_helper.update_secret_dict("new", lambda d: d.update(a="1"))
    assert len(kv.writes) == 1  # unchanged content → no new KV version


def test_update_gives_up_after_persistent_conflicts(kv):
    kv.conflict_writes = 99
    with pytest.raises(kv_helper.VaultCasConflict):
        kv_helper.update_secret_dict("p", lambda d: d.update(a="1"))
    assert kv.writes == []


# --- ctx.set_secret -------------------------------------------------------------

def test_set_secret_does_not_write_after_failed_read(kv):
    path = f"plugins/{PID}"
    kv.store[path] = ({"db_password": "dummy-password-not-real"}, 1)
    kv.fail_reads = 1

    assert _ctx().set_secret("api_token", "dummy-token-not-real") is False

    assert kv.writes == []
    assert kv.store[path][0] == {"db_password": "dummy-password-not-real"}


def test_set_secret_keeps_other_keys(kv):
    path = f"plugins/{PID}"
    kv.store[path] = ({"db_password": "dummy-password-not-real"}, 1)
    ctx = _ctx()

    assert ctx.set_secret("api_token", "dummy-token-not-real") is True
    assert kv.store[path][0] == {
        "db_password": "dummy-password-not-real", "api_token": "dummy-token-not-real",
    }
    assert kv.writes[-1][2] == 1  # cas = version that was read
    assert asyncio.run(ctx.aget_secret("api_token")) == "dummy-token-not-real"
    assert asyncio.run(ctx.aset_secret("third", "x")) is True


# --- system config --------------------------------------------------------------

def test_system_config_apply_aborts_on_read_error(kv):
    kv.store["core/settings"] = ({"system_api_key": "dummy-key-not-real", "github_token": "dummy"}, 5)
    kv.fail_reads = 1

    with pytest.raises(RuntimeError):
        system_config_service.apply({"gitlab_token": "dummy-gitlab-not-real"}, apply_runtime=False)

    assert kv.writes == []
    assert kv.store["core/settings"][0]["system_api_key"] == "dummy-key-not-real"


def test_system_config_apply_merges(kv):
    kv.store["core/settings"] = ({"system_api_key": "dummy-key-not-real"}, 5)
    system_config_service.apply(
        {"gitlab_token": "dummy-gitlab-not-real"}, apply_runtime=False, remove_keys=["missing"],
    )
    assert kv.store["core/settings"][0] == {
        "system_api_key": "dummy-key-not-real", "gitlab_token": "dummy-gitlab-not-real",
    }
    assert kv.writes[-1][2] == 5


# --- plugin settings PUT -----------------------------------------------------------

def _put(monkeypatch, values):
    schema = [
        PluginSettingField(key="url", label="U", kind="text"),
        PluginSettingField(key="token", label="T", kind="secret"),
        PluginSettingField(key="other_secret", label="O", kind="secret"),
    ]
    entry = {"manifest": SimpleNamespace(settings_schema=schema)}
    monkeypatch.setattr(plugins_api, "_manager", lambda: SimpleNamespace(registry={PID: entry}))
    monkeypatch.setattr(plugins_api.bus, "emit", lambda *a, **k: None)
    payload = plugins_api.PluginSettingsUpdate(values=values)
    identity = SimpleNamespace(username="tester")
    return asyncio.run(plugins_api.update_plugin_settings(PID, payload, identity))


def test_plugin_settings_put_aborts_on_read_error(kv, monkeypatch):
    path = f"plugins/{PID}/settings"
    kv.store[path] = ({"url": "http://old", "token": "dummy-token-not-real"}, 2)
    kv.fail_reads = 1

    with pytest.raises(HTTPException) as exc:
        _put(monkeypatch, {"url": "http://new", "token": ""})

    assert exc.value.status_code == 503 and "nothing was saved" in exc.value.detail
    assert kv.writes == []


def test_plugin_settings_put_merges_and_never_stores_blank_secrets(kv, monkeypatch):
    path = f"plugins/{PID}/settings"
    kv.store[path] = ({"url": "http://old", "token": "dummy-token-not-real", "legacy": "keep-me"}, 2)

    out = _put(monkeypatch, {"url": "http://new", "token": "", "other_secret": ""})

    assert kv.store[path][0] == {
        "url": "http://new",                  # submitted → updated
        "token": "dummy-token-not-real",      # masked/blank → kept
        "legacy": "keep-me",                  # not submitted → kept
    }                                         # other_secret: blank + unset → not stored as ""
    assert sorted(out["saved"]) == ["token", "url"]
    assert kv.writes[-1][2] == 2


def test_plugin_settings_put_rejects_bad_payload_before_vault(kv, monkeypatch):
    kv.fail_reads = 1
    with pytest.raises(HTTPException) as exc:
        _put(monkeypatch, {"unknown": "1"})
    assert exc.value.status_code == 400
    assert kv.fail_reads == 1  # Vault was not even asked


# --- system API key cache (BG-17) ---------------------------------------------------

def test_system_api_key_failed_read_is_not_cached_as_absent(kv, monkeypatch):
    monkeypatch.delenv("LYNDRIX_SYSTEM_API_KEY", raising=False)
    clock = {"now": 1000.0}
    monkeypatch.setattr(security.time, "monotonic", lambda: clock["now"])
    security.invalidate_system_api_key_cache()
    kv.store["core/settings"] = ({"system_api_key": "dummy-key-not-real"}, 1)

    kv.fail_reads = 1
    assert security.resolve_system_api_key() is None      # Vault hiccup
    clock["now"] += security._KEY_ERROR_RETRY_SECONDS + 0.1
    # Long before the regular TTL the key is back — the error was not cached as "unset".
    assert security.resolve_system_api_key() == "dummy-key-not-real"

    security.invalidate_system_api_key_cache()
    monkeypatch.setattr(core.services.vault_instance, "is_connected", False)
    assert security.resolve_system_api_key() is None      # sealed/unknown
    monkeypatch.setattr(core.services.vault_instance, "is_connected", True)
    assert security.resolve_system_api_key() == "dummy-key-not-real"   # no negative cache
    security.invalidate_system_api_key_cache()
