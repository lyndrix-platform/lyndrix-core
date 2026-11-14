"""Permission-gated toasts resolve their audience off the event loop (BG-12 #10)."""
import asyncio
import threading

import core.services  # noqa: F401  (import order: services before api)
from core.components.auth.logic import access_service
from core.components.notifications.logic import notification_service as ns_mod


def test_gated_toast_reaches_permitted_sessions_and_resolves_in_a_thread(monkeypatch):
    svc = ns_mod.NotificationService()
    sessions = [("c-admin", "root", True), ("c-op", "operator", False),
                ("c-view", "viewer", False), ("c-op2", "operator", False), ("c-anon", "", False)]
    notified: list[str] = []
    lookups: list[tuple[str, bool]] = []

    def has_permission(username, permission):
        lookups.append((username, threading.current_thread() is threading.main_thread()))
        return username == "operator" and permission == "feature:plugins.manage"

    monkeypatch.setattr(svc, "_connected_sessions", lambda: sessions)
    monkeypatch.setattr(svc, "_notify_client", lambda client, message, toast_type: notified.append(client))
    monkeypatch.setattr(access_service, "username_has_permission", has_permission)

    asyncio.run(svc.broadcast_toast_async("install failed", "negative", required_permission="feature:plugins.manage"))

    assert notified == ["c-admin", "c-op", "c-op2"]
    assert sorted(u for u, _ in lookups) == ["operator", "viewer"]      # once per user, not per client
    assert not any(on_main for _u, on_main in lookups)                  # never on the event loop thread


def test_ungated_toast_uses_the_plain_broadcast(monkeypatch):
    svc = ns_mod.NotificationService()
    calls: list[tuple] = []
    monkeypatch.setattr(svc, "broadcast_toast", lambda message, type_, **kw: calls.append((message, type_, kw)))
    asyncio.run(svc.broadcast_toast_async("hello", "info"))
    assert calls == [("hello", "info", {})]
