"""Configuration hierarchy and settings that must follow later changes (BG-20).

Documented order: OS env > .env > Vault > default.
"""
import logging
from types import SimpleNamespace

import pytest

import config as config_mod
import core.services  # noqa: F401  (import order: services before api)
from config import Settings
from core import i18n
from core import logger as logger_mod
from core.components.plugins.logic import plugin_service as ps_mod


@pytest.fixture()
def vault(monkeypatch):
    """Fake Vault holding ``core/settings``; returns the dict to fill."""
    stored: dict = {}

    def read_secret_version(path, mount_point="secret", **_kw):
        assert path == "core/settings"
        return {"data": {"data": dict(stored)}}

    client = SimpleNamespace(secrets=SimpleNamespace(kv=SimpleNamespace(v2=SimpleNamespace(
        read_secret_version=read_secret_version))))
    monkeypatch.setattr(core.services.vault_instance, "client", client)
    monkeypatch.setattr(core.services.vault_instance, "is_connected", True)
    return stored


@pytest.fixture()
def dotenv(tmp_path, monkeypatch):
    """Run with a ``.env`` in a temp cwd; returns a function building fresh Settings."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(config_mod, "_DOTENV_FIELDS", None)
    for name in ("APP_TITLE", "LOG_LEVEL", "DEFAULT_LOCALE", "THEME_ENGINE_ENABLED",
                 "LYNDRIX_PLUGIN_UPDATE_CHECK_INTERVAL_S", "LYNDRIX_EVENT_POLL_MS"):
        monkeypatch.delenv(name, raising=False)

    def build(content: str) -> Settings:
        (tmp_path / ".env").write_text(content)
        config_mod._DOTENV_FIELDS = None
        return Settings()

    yield build
    config_mod._DOTENV_FIELDS = None


def test_dotenv_value_is_not_overwritten_by_vault(dotenv, vault):
    s = dotenv("APP_TITLE=From Dotenv\n")
    vault.update({"APP_TITLE": "From Vault", "DEFAULT_LOCALE": "en"})

    applied = s.hydrate_from_vault()

    assert s.APP_TITLE == "From Dotenv"            # .env outranks Vault
    assert s.DEFAULT_LOCALE == "en"                # not in .env → Vault applies
    assert applied == 1
    assert s.is_env_locked("APP_TITLE") and not s.is_env_locked("DEFAULT_LOCALE")


def test_os_env_still_wins_and_locks(dotenv, vault, monkeypatch):
    monkeypatch.setenv("APP_TITLE", "From Env")
    s = dotenv("")
    vault["APP_TITLE"] = "From Vault"
    assert s.hydrate_from_vault() == 0
    assert s.APP_TITLE == "From Env" and s.is_env_locked("APP_TITLE")


def test_dotenv_lock_is_case_insensitive_like_pydantic_settings(dotenv):
    s = dotenv("app_title=lower case key\n")
    assert s.APP_TITLE == "lower case key" and s.is_env_locked("APP_TITLE")


def test_invalid_vault_values_are_skipped_with_a_warning(dotenv, vault, caplog):
    s = dotenv("")
    before = (s.LOG_LEVEL, s.LYNDRIX_PLUGIN_UPDATE_CHECK_INTERVAL_S)
    vault.update({
        "LOG_LEVEL": "foo",                         # select outside its options
        "LYNDRIX_PLUGIN_UPDATE_CHECK_INTERVAL_S": "not-a-number",   # int that does not parse
        "APP_TITLE": "Valid Title",
    })
    with caplog.at_level(logging.WARNING, logger="Core:Config"):
        applied = s.hydrate_from_vault()

    assert (s.LOG_LEVEL, s.LYNDRIX_PLUGIN_UPDATE_CHECK_INTERVAL_S) == before   # never reach the live settings
    assert s.APP_TITLE == "Valid Title" and applied == 1
    text = caplog.text
    assert "LOG_LEVEL" in text and "LYNDRIX_PLUGIN_UPDATE_CHECK_INTERVAL_S" in text


def test_value_rejected_by_a_model_rule_does_not_crash_or_apply(dotenv, vault, monkeypatch):
    s = dotenv("")
    spec = config_mod.EditableSetting("LYNDRIX_EVENT_POLL_MS", "Poll", "test only", kind="int")
    monkeypatch.setattr(config_mod, "EDITABLE_SETTINGS", [*config_mod.EDITABLE_SETTINGS, spec])
    before = s.LYNDRIX_EVENT_POLL_MS
    vault.update({"LYNDRIX_EVENT_POLL_MS": "5", "APP_TITLE": "Still Applied?"})   # < 100 → model validator

    s.hydrate_from_vault()                          # must not raise

    assert s.LYNDRIX_EVENT_POLL_MS == before


def test_hydrated_values_keep_their_python_types(dotenv, vault):
    s = dotenv("")
    vault.update({"THEME_ENGINE_ENABLED": "false", "LYNDRIX_PLUGIN_UPDATE_CHECK_INTERVAL_S": "900"})
    s.hydrate_from_vault()
    assert s.THEME_ENGINE_ENABLED is False and s.LYNDRIX_PLUGIN_UPDATE_CHECK_INTERVAL_S == 900


# --- import-time captures -----------------------------------------------------------

def test_plugin_collection_url_follows_the_setting(monkeypatch):
    monkeypatch.delenv("PLUGIN_COLLECTION_URL", raising=False)
    monkeypatch.setattr(ps_mod.settings, "LYNDRIX_PLUGIN_COLLECTION_URL", "https://github.com/acme/my-collection.git")

    assert ps_mod.collection_repo_url() == "https://github.com/acme/my-collection.git"
    assert ps_mod.collection_repo_id() == "my-collection"
    assert ps_mod.collection_json_path().parts[-3:] == ("my-collection", "plugin-directory", "plugins.json")
    assert ps_mod.collection_fallback_url() == (
        "https://raw.githubusercontent.com/acme/my-collection/main/plugin-directory/plugins.json"
    )
    # The old module constants still resolve — to the CURRENT value.
    assert ps_mod.COLLECTION_REPO_URL == "https://github.com/acme/my-collection.git"
    assert ps_mod.COLLECTION_REPO_ID == "my-collection"
    assert ps_mod.COLLECTION_JSON_PATH == ps_mod.collection_json_path()

    monkeypatch.setenv("PLUGIN_COLLECTION_URL", "https://mirror.example/plugins.json")
    assert ps_mod.collection_fallback_url() == "https://mirror.example/plugins.json"


def test_i18n_locales_follow_the_setting(monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "DEFAULT_LOCALE", "en")
    monkeypatch.setattr(settings, "SUPPORTED_LOCALES", "en,de")
    assert i18n._get_supported() == {"en", "de"} and i18n._default == "en"

    monkeypatch.setattr(settings, "DEFAULT_LOCALE", "de")          # e.g. Vault hydration after first use
    monkeypatch.setattr(settings, "SUPPORTED_LOCALES", "de")
    assert i18n._get_supported() == {"de"}
    assert i18n._current_locale() == "de"                           # no NiceGUI request → the default


# --- LOG_LEVEL -------------------------------------------------------------------------

def test_log_level_is_applied_and_tolerates_garbage(monkeypatch):
    root = logging.getLogger()
    saved = root.level
    try:
        monkeypatch.setattr(logger_mod, "IS_DEBUG", False)
        logger_mod.apply_log_level("WARNING")
        assert root.level == logging.WARNING
        logger_mod.apply_log_level("debug")
        assert root.level == logging.DEBUG
        logger_mod.apply_log_level("foo")                            # invalid → INFO, never a crash
        assert root.level == logging.INFO
        monkeypatch.setattr(logger_mod, "IS_DEBUG", True)            # LYNDRIX_DEBUG=true wins
        logger_mod.apply_log_level("ERROR")
        assert root.level == logging.DEBUG
    finally:
        root.setLevel(saved)


def test_plugin_teardown_timeout_is_a_declared_setting():
    assert Settings.model_fields["LYNDRIX_PLUGIN_TEARDOWN_TIMEOUT"].default == 15
