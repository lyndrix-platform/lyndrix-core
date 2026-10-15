"""Runtime sidebar entries (core API 1.9): NavRegistry, ctx.register_nav_entries,
dispose() cleanup and the /api/nav visibility filter."""
from types import SimpleNamespace

import pytest

from core.bus import bus
from core.components.plugins.api.nav_api import _visible_plugins
from core.components.plugins.logic.context import ModuleContext
from core.components.plugins.logic.models import ModuleManifest, ModulePermissions
from core.components.plugins.logic.nav import NAV_CHANGED_TOPIC, NavRegistry, nav_registry

PID = "lyndrix.plugin.test_nav"


def _ctx(plugin_id: str = PID) -> ModuleContext:
    manifest = ModuleManifest(
        id=plugin_id, name="Nav Tester", version="0.0.1", type="PLUGIN",
        permissions=ModulePermissions(emit=[], subscribe=[]),
    )
    return ModuleContext(manifest)


@pytest.fixture()
def changes():
    seen: list[dict] = []

    def _listener(payload):
        seen.append(payload)

    _listener.__qualname__ = f"test_nav_listener<{id(seen)}>"
    bus.subscribe(NAV_CHANGED_TOPIC)(_listener)
    try:
        yield seen
    finally:
        bus.unsubscribe(NAV_CHANGED_TOPIC, _listener)
        nav_registry.clear(PID)


def test_register_validates_sorts_and_announces_once(changes):
    reg = NavRegistry()
    n = reg.register("p", [
        {"id": "zeta", "label": "Zeta", "url": "https://zeta.example", "open_mode": "new_tab", "order": 1},
        {"id": "alpha", "label": "Alpha", "url": "/external/alpha", "icon": "insights"},
        {"id": "bad id", "label": "x", "url": "https://x"},            # invalid id
        {"id": "nourl", "label": "x", "url": "ftp://nope"},           # invalid url
        {"id": "mode", "label": "x", "url": "https://x", "open_mode": "popup"},  # invalid mode
    ])
    assert n == 2
    entries = reg.entries()
    assert [e["id"] for e in entries] == ["alpha", "zeta"]          # same group → order, then label
    assert entries[0]["group"] == "Services" and entries[0]["icon"] == "insights"
    assert entries[0]["open_mode"] == "iframe" and entries[1]["open_mode"] == "new_tab"
    assert entries[0]["plugin_id"] == "p"
    assert [c["count"] for c in changes if c["plugin_id"] == "p"] == [2]

    # Same set again → no second announcement; a changed set → one more.
    reg.register("p", [
        {"id": "alpha", "label": "Alpha", "url": "/external/alpha", "icon": "insights"},
        {"id": "zeta", "label": "Zeta", "url": "https://zeta.example", "open_mode": "new_tab", "order": 1},
    ])
    assert [c["count"] for c in changes if c["plugin_id"] == "p"] == [2]
    reg.register("p", [])
    assert reg.entries() == []
    assert [c["count"] for c in changes if c["plugin_id"] == "p"] == [2, 0]


def test_entries_can_be_limited_to_visible_plugins():
    reg = NavRegistry()
    reg.register("a", [{"id": "one", "label": "One", "url": "https://one"}])
    reg.register("b", [{"id": "two", "label": "Two", "url": "https://two"}])
    assert [e["id"] for e in reg.entries(["b"])] == ["two"]
    assert reg.entries([]) == []
    assert reg.plugin_ids() == ["a", "b"]


def test_context_registers_and_dispose_clears(changes):
    ctx = _ctx()
    assert ctx.register_nav_entries([{"id": "ha", "label": "Home Assistant", "url": "https://ha.local"}]) == 1
    assert [e["id"] for e in nav_registry.entries([PID])] == ["ha"]
    ctx.dispose()
    assert nav_registry.entries([PID]) == []
    assert [c["count"] for c in changes if c["plugin_id"] == PID] == [1, 0]


def test_visibility_filter_keeps_everything_for_superadmin_and_system():
    ident = SimpleNamespace(is_system=True, roles=[], username="svc")
    assert _visible_plugins(ident, ["a", "b"]) == ["a", "b"]
    ident = SimpleNamespace(is_system=False, roles=["superadmin"], username="root")
    assert _visible_plugins(ident, ["a"]) == ["a"]
