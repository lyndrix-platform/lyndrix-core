"""Panels (core API 1.11): manifest validation of ``panels`` / ``ai_instructions``,
``PluginOut`` carrying ``panels`` (and never ``ai_instructions``), and the
read-only ``core.api.module_registry`` view.

Run inside the lyndrix-core runtime:
    pytest tests/plugins/test_panels_manifest.py -v
"""
import pytest
from pydantic import ValidationError

from core.components.plugins.logic.models import ModuleManifest

PID = "lyndrix.plugin.panel_test"


def _manifest(**kw) -> ModuleManifest:
    return ModuleManifest(id=PID, name="Panel Tester", version="0.0.1", type="PLUGIN", **kw)


# ─── panels ──────────────────────────────────────────────────────────────────

def test_manifest_without_panels_is_unchanged():
    m = _manifest()
    assert m.panels == []
    assert m.ai_instructions is None


def test_valid_panel_declarations_are_normalised():
    m = _manifest(panels=[
        {"id": "chat", "label": " Assistant ", "icon": " smart_toy ", "description": "Ask the AI",
         "default_size": {"width": 420, "height": 560},
         "permission": "plugin:lyndrix.plugin.panel_test:api:read"},
        {"id": "status-2", "label": "Status"},                      # minimal → icon defaults
    ])
    assert m.panels[0] == {
        "id": "chat", "label": "Assistant", "icon": "smart_toy", "description": "Ask the AI",
        "default_size": {"width": 420, "height": 560},
        "permission": "plugin:lyndrix.plugin.panel_test:api:read",
    }
    assert m.panels[1] == {"id": "status-2", "label": "Status", "icon": "extension"}


@pytest.mark.parametrize("panel", [
    {"label": "no id"},
    {"id": "Chat", "label": "upper case"},
    {"id": "1abc", "label": "digit first"},
    {"id": "a" * 42, "label": "too long"},
    {"id": "chat"},                                                  # label missing
    {"id": "chat", "label": "   "},
    {"id": "chat", "label": "x", "icon": ""},
    {"id": "chat", "label": "x", "description": 5},
    {"id": "chat", "label": "x", "default_size": [420, 560]},
    {"id": "chat", "label": "x", "default_size": {"width": 420}},
    {"id": "chat", "label": "x", "default_size": {"width": "420", "height": 560}},
    {"id": "chat", "label": "x", "default_size": {"width": 0, "height": 10}},
    {"id": "chat", "label": "x", "default_size": {"width": True, "height": 10}},
    {"id": "chat", "label": "x", "permission": ""},
    "chat",
])
def test_invalid_panel_declarations_are_rejected(panel):
    with pytest.raises(ValidationError):
        _manifest(panels=[panel])


def test_duplicate_panel_ids_are_rejected():
    with pytest.raises(ValidationError, match="declared twice"):
        _manifest(panels=[{"id": "chat", "label": "A"}, {"id": "chat", "label": "B"}])


# ─── ai_instructions ─────────────────────────────────────────────────────────

def test_ai_instructions_accepts_markdown_and_caps_the_size():
    assert _manifest(ai_instructions="# Hello\n\nUse `GET /status`.").ai_instructions.startswith("# Hello")
    assert _manifest(ai_instructions="x" * 20_000).ai_instructions is not None
    with pytest.raises(ValidationError):
        _manifest(ai_instructions="x" * 20_001)
    with pytest.raises(ValidationError):
        _manifest(ai_instructions=["not", "a", "string"])


# ─── PluginOut ───────────────────────────────────────────────────────────────

def test_plugin_out_carries_panels_but_not_ai_instructions(monkeypatch):
    from core.components.plugins.api import plugins_api

    class _Svc:
        def latest_version_info(self, module_id, version):
            return None, False

    class _Mgr:
        def get_runtime(self, module_id):
            return None

    monkeypatch.setattr(plugins_api, "_plugin_service", lambda: _Svc())
    monkeypatch.setattr(plugins_api, "_manager", lambda: _Mgr())

    m = _manifest(panels=[{"id": "chat", "label": "Chat"}], ai_instructions="for the assistant only")
    out = plugins_api._to_plugin_out(m.id, {"manifest": m, "status": "active"}).model_dump()
    assert out["panels"] == [{"id": "chat", "label": "Chat", "icon": "extension"}]
    assert "ai_instructions" not in out

    plain = plugins_api._to_plugin_out(PID, {"manifest": _manifest(), "status": "active"}).model_dump()
    assert plain["panels"] == []


# ─── core.api.module_registry ────────────────────────────────────────────────

def test_module_registry_view_exposes_manifests_read_only(monkeypatch):
    import core.services  # noqa: F401  (must precede core.api — circular import)
    import core.api as core_api
    from core.components.plugins.logic.manager import ModuleManager

    mgr = ModuleManager()
    a = _manifest(panels=[{"id": "chat", "label": "Chat"}])
    b = ModuleManifest(id="lyndrix.plugin.other", name="Other", version="0.0.1", type="PLUGIN",
                       ai_instructions="# Other\nTalks about itself.")
    mgr.registry[a.id] = {"manifest": a, "status": "active"}
    mgr.registry[b.id] = {"manifest": b, "status": "disabled"}
    monkeypatch.setattr(core_api._ModuleRegistryView, "_manager", staticmethod(lambda: mgr))

    view = core_api.module_registry
    assert "module_registry" in core_api.__all__
    assert {m.id for m in view.get_manifests()} == {a.id, b.id}
    assert view.get(b.id) is b and view.get(b.id).ai_instructions.startswith("# Other")
    assert view.get("lyndrix.plugin.nope") is None
    assert view.is_active(a.id) is True
    assert view.is_active(b.id) is False
    assert view.is_active("lyndrix.plugin.nope") is False
    # A view, not the manager: nothing that loads or mutates modules leaks through.
    for forbidden in ("registry", "load_module", "enable", "disable", "unload"):
        assert not hasattr(view, forbidden), forbidden
