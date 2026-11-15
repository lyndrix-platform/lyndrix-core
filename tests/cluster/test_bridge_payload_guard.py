"""SC-06 (bridge part): an event whose payload carries a secret is delivered
locally but never published to the cluster (= never persisted in cluster_events)."""
import asyncio
import logging

import pytest

from core.bus import bus
from core.components.cluster.logic import payload_guard
from core.components.cluster.logic.bridge import ClusterBridge
from core.logger import find_sensitive, has_secret_shape

FAKE = "dummy-token-not-real"
PEM = "-----BEGIN OPENSSH PRIVATE KEY-----\nZHVtbXktbm90LXJlYWw=\n-----END OPENSSH PRIVATE KEY-----"


# --- payloads the core and the first-party plugins bridge today: all pass --------

LEGITIMATE_PAYLOADS = [
    ("plugin:state_changed", {"plugin_id": "lyndrix.plugin.itsm", "action": "installed"}),
    ("plugin:state_changed", {"plugin_id": "x", "is_active": True}),
    ("plugin:desired_changed", {"plugin_id": "x", "action": "uninstalled"}),
    ("plugin:installed", {"repo": "lyndrix-plugin-itsm", "path": "/app/plugins/lyndrix_plugin_itsm"}),
    ("plugin:install_failed", {"repo": "x", "error": "verification_failed_auto_reverted"}),
    ("plugin:install_failed", {"repo": "x", "error": "Client error '404 Not Found' for url "
                                                     "'https://github.com/o/r/archive/refs/tags/v9.zip'"}),
    ("plugin:install_failed", {"repo": "x", "error": "Invalid token: expired"}),
    ("plugin:update_available", {"plugin_id": "x", "current_version": "1.0.0", "latest_version": "v1.1.0"}),
    # key NAMES of changed settings — names only, no values
    ("plugin:settings_changed", {"plugin_id": "x", "keys": ["api_token", "webhook_url"], "by": "admin"}),
    ("settings:changed", {"keys": ["github_token", "system_api_key"], "removed": ["gitlab_token"]}),
    ("plugin:runtime_status", {"plugin_id": "x", "phase": "starting", "message": "Initial probe sweep",
                               "progress": [3, 10], "blocking": True}),
    ("plugin:operation_changed", {"operation_id": 7, "status": "running", "done": 2, "total": 6}),
    ("ui:needs_refresh", {"reason": "theme_changed"}),
    ("notification:new", {"id": "n1", "title": "Plugin installed", "message": "ITSM 1.2.0 is active",
                          "type": "positive", "timestamp": 1731580000.0}),
    ("system:restart_requested", {"by": "admin", "order": ["core-2", "core-1"]}),
    ("cluster:lease_released", {"name": "monitoring:prober"}),
    ("monitoring:monitors_changed", {"reason": "import", "count": 12, "instance_id": "core-1-ab12"}),
    ("docker_manager:hosts_updated", {"reason": "sync", "ids": [1, 2, 3]}),
    ("entity_registry:maintenance_changed", {"source": "itsm", "kind": "window", "count": 3,
                                             "skipped_unknown": 0, "skipped_invalid": 0}),
    ("server_manager:catalog_changed", {"catalog_dir": "/data/storage/catalog", "action": "reload"}),
    ("external_services:changed", {"action": "update", "slug": "grafana"}),
    ("meeting_bingo:settings", {"scoreboard_enabled": True}),
    ("iac:job_queued", {"job_id": 4711}),
    ("itsm:catalog_reloaded", {"reloaded_at": "2026-11-14T10:00:00+00:00"}),
    # descriptive names and empty secrets are not secrets
    ("x:y", {"has_token": True, "token_count": 3, "secret_ref": "plugins/x/git_token", "password": "",
             "api_key": None, "auth_type": "token", "key": "sort_order", "sort_key": "name"}),
    ("x:y", {"url": "https://gitlab.example.invalid/group/repo.git", "remote": "ssh://git@github.com/o/r.git"}),
]


@pytest.mark.parametrize("topic,payload", LEGITIMATE_PAYLOADS)
def test_todays_bridged_payloads_are_not_affected(topic, payload):
    assert find_sensitive(payload) is None, topic
    assert payload_guard.blocks(topic, payload) is False


# --- payloads that must stay local -------------------------------------------------

SENSITIVE_PAYLOADS = [
    ({"repo_id": "r", "url": "https://gitlab.example.invalid/r.git", "secret_value": FAKE}, "secret_value"),
    ({"auth": {"type": "ssh", "private_key": PEM}}, "auth.private_key"),
    ({"master_key": FAKE}, "master_key"),
    ({"config": {"provider": "discord", "webhook_url": "https://discord.invalid/api/webhooks/1/x"}}, "config.webhook_url"),
    ({"args": {"env_vars": [{"key": "LYNDRIX_AI_BRIDGE_TOKEN", "value": FAKE}]}}, "args.env_vars"),
    ({"env": {"GITLAB_TOKEN": FAKE}}, "env"),
    ({"items": [{"name": "a"}, {"name": "b", "client_secret": FAKE}]}, "items[1].client_secret"),
    ({"headers": {"Authorization": "Bearer abcdefgh12345678"}}, "headers.Authorization"),
    ({"pairs": [{"key": "API_TOKEN", "value": FAKE}]}, "pairs[0].value"),
    # values that are shaped like a credential, under harmless names
    ({"url": f"https://oauth2:{FAKE}@gitlab.example.invalid/g/r.git"}, "url"),
    ({"error": "git clone failed for https://x:" + FAKE + "@host.invalid/r"}, "error"),
    ({"note": "use ghp_" + "a1B2c3D4e5F6g7H8i9J0"}, "note"),
    ({"lines": ["ok", "PRIVATE-TOKEN: " + "glpat-" + "a1B2c3D4e5F6g7H8"]}, "lines[1]"),
    ({"data": {"id_token": "eyJhbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiJkdW1teSJ9" + ".ZHVtbXktc2lnbmF0dXJl"}}, "data.id_token"),
]


@pytest.mark.parametrize("payload,field", SENSITIVE_PAYLOADS)
def test_secret_carrying_payloads_are_located_by_field_name(payload, field):
    assert find_sensitive(payload) == field


def test_located_field_never_contains_the_value():
    for payload, _field in SENSITIVE_PAYLOADS:
        where = find_sensitive(payload)
        assert FAKE not in where and "PRIVATE KEY" not in where


def test_value_shapes():
    assert has_secret_shape("https://user:pw@host.invalid/x")
    assert has_secret_shape("Authorization: Basic ZHVtbXk6ZHVtbXk=")
    assert not has_secret_shape("Invalid token: expired")
    assert not has_secret_shape("https://user@host.invalid/x")      # login only, no password
    assert not has_secret_shape("at 12:30:01 status=ok")
    assert not has_secret_shape("")


def test_deeply_nested_payload_does_not_recurse_forever():
    payload = current = {}
    for _ in range(50):
        current["next"] = {}
        current = current["next"]
    current["token"] = FAKE
    assert find_sensitive(payload) is None  # beyond the depth limit: bounded, no crash


# --- bridge integration --------------------------------------------------------------

class RecordingCoordinator:
    driver = "fake"
    instance_id = "me"

    def __init__(self):
        self.published = []

    async def publish(self, topic, payload):
        self.published.append((topic, payload))

    def subscribe(self, callback):
        pass


def test_bridge_keeps_secret_events_local_and_warns_without_the_value(caplog, monkeypatch):
    monkeypatch.setattr(payload_guard, "_last_warned", {})
    coord = RecordingCoordinator()
    bridge = ClusterBridge(coord, "me")
    seen_locally = []
    topic = "test_guard:sync_requested"
    bus.subscribe(topic)(lambda payload: seen_locally.append(payload))

    async def run():
        bridge.attach()
        try:
            with caplog.at_level(logging.WARNING, logger="Core:Cluster:Bridge"):
                for _ in range(5):                                   # chatty emitter
                    bus.emit(topic, {"repo_id": "r", "secret_value": FAKE}, cluster=True)
                bus.emit(topic, {"repo_id": "r"}, cluster=True)      # the clean variant goes out
                bus.emit("plugin:installed", {"repo": "x", "path": "/p", "token": FAKE})  # allowlisted topic
                await asyncio.sleep(0.05)
        finally:
            bridge.detach()

    asyncio.run(run())

    assert coord.published == [(topic, {"repo_id": "r"})]
    assert len(seen_locally) == 6                                    # local delivery untouched
    warnings = [r.getMessage() for r in caplog.records if "NOT published" in r.getMessage()]
    assert len(warnings) == 2                                        # rate-limited: one per (topic, field)
    assert any(topic in w and "secret_value" in w for w in warnings)
    assert any("plugin:installed" in w and "'token'" in w for w in warnings)
    assert all(FAKE not in w for w in warnings)


def test_warning_repeats_after_the_interval(monkeypatch, caplog):
    monkeypatch.setattr(payload_guard, "_last_warned", {})
    payload = {"password": FAKE}
    with caplog.at_level(logging.WARNING, logger="Core:Cluster:Bridge"):
        assert payload_guard.blocks("t:x", payload, now=1000.0)
        assert payload_guard.blocks("t:x", payload, now=1100.0)
        assert payload_guard.blocks("t:x", payload, now=1000.0 + payload_guard.WARN_INTERVAL_SECONDS + 1)
    assert len([r for r in caplog.records if "NOT published" in r.getMessage()]) == 2
