"""The Vault setup/unseal pages subscribe a closure per page render; it must
leave the bus again when the client goes away.

Since the bus delivers to every distinct callback (BG-14), each render adds a
subscriber — without the cleanup every closed tab left one behind that kept
updating a dead label.
"""
import types
from unittest.mock import MagicMock

import pytest

import core.services  # noqa: F401  (import order: services before api)
from core.bus import bus
from core.components.vault.ui import setup_ui, unseal_ui


class _Client:
    """NiceGUI client double: records the handlers registered for its deletion."""
    id = "client-1"

    def __init__(self):
        self.delete_handlers = []

    def on_delete(self, handler):
        self.delete_handlers.append(handler)

    def on_disconnect(self, handler):  # pragma: no cover - on_delete is preferred
        raise AssertionError("on_delete must be preferred when available")


class _OldClient:
    """NiceGUI without Client.on_delete."""
    id = "client-old"

    def __init__(self):
        self.delete_handlers = []

    def on_disconnect(self, handler):
        self.delete_handlers.append(handler)


def _handlers(topic: str, name: str) -> list:
    return [cb for cb in bus.subscribers.get(topic, []) if getattr(cb, "__name__", "") == name]


@pytest.mark.parametrize("module, render, topic, handler_name", [
    (unseal_ui, "render_unseal_page", "vault:unseal_failed", "on_vault_failed"),
    (setup_ui, "render_setup_wizard", "vault:init_failed", "on_init_failed"),
])
@pytest.mark.parametrize("client_cls", [_Client, _OldClient])
def test_page_handler_is_unsubscribed_when_the_client_goes_away(
        monkeypatch, module, render, topic, handler_name, client_cls):
    monkeypatch.setattr(module, "ui", MagicMock())
    monkeypatch.setattr(module, "apply_theme", lambda **kw: None)
    if hasattr(module, "attach_maintenance_overlay"):
        monkeypatch.setattr(module, "attach_maintenance_overlay", lambda: None)
    before = len(_handlers(topic, handler_name))
    clients = [client_cls(), client_cls()]

    for client in clients:                                   # two tabs open the page
        monkeypatch.setattr(module, "context", types.SimpleNamespace(client=client))
        getattr(module, render)()
    assert len(_handlers(topic, handler_name)) == before + 2
    assert [len(c.delete_handlers) for c in clients] == [1, 1]

    clients[0].delete_handlers[0]()                          # first tab closes
    assert len(_handlers(topic, handler_name)) == before + 1
    clients[1].delete_handlers[0]()
    assert len(_handlers(topic, handler_name)) == before
