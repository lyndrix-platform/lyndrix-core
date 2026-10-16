"""AI discovery index (API 1.12): built from manifests only, no plugin knowledge in core."""
import pytest

from core.components.plugins.api.ai_api import build_plugin_index
from core.components.plugins.logic.models import ModuleManifest


def _m(**kw):
    base = dict(id="lyndrix.plugin.x", name="X", version="1.0.0", type="PLUGIN", description="d", author="a")
    base.update(kw)
    return ModuleManifest(**base)


def test_index_lists_only_self_describing_active_or_remote_plugins():
    manifests = [
        _m(id="lyndrix.plugin.full", name="Full", version="2.1.0", ai_instructions="Purpose.", ai_instructions_route="/ai/instructions"),
        _m(id="lyndrix.plugin.purpose_only", name="P", ai_instructions="Only a purpose."),
        _m(id="lyndrix.plugin.route_only", name="R", ai_instructions_route="/ai/instructions/"),
        _m(id="lyndrix.plugin.silent", name="S"),
        _m(id="lyndrix.plugin.disabled", name="D", ai_instructions="x"),
        _m(id="lyndrix.plugin.remote", name="Rem", ai_instructions="pinned elsewhere", ai_instructions_route="/ai/instructions"),
        _m(id="core.something", name="Core", type="CORE", ai_instructions="core component"),
    ]
    status = {"lyndrix.plugin.full": "active", "lyndrix.plugin.purpose_only": "active", "lyndrix.plugin.route_only": "active",
              "lyndrix.plugin.silent": "active", "lyndrix.plugin.disabled": "disabled", "lyndrix.plugin.remote": "remote",
              "core.something": "active"}
    index = build_plugin_index(manifests, status.get)
    ids = [e["id"] for e in index]
    assert ids == ["lyndrix.plugin.full", "lyndrix.plugin.purpose_only", "lyndrix.plugin.remote", "lyndrix.plugin.route_only"]
    full = index[0]
    assert full == {"id": "lyndrix.plugin.full", "name": "Full", "version": "2.1.0", "status": "active",
                    "purpose": "Purpose.", "instructions_url": "/api/plugins/lyndrix.plugin.full/ai/instructions"}
    assert index[1]["instructions_url"] is None
    # trailing slash normalised by the manifest validator
    assert index[3]["instructions_url"] == "/api/plugins/lyndrix.plugin.route_only/ai/instructions"
    assert index[2]["status"] == "remote"


@pytest.mark.parametrize("bad", ["ai/instructions", "/ai/../x", "/ai/in structions", "http://x/y"])
def test_route_validation(bad):
    with pytest.raises(ValueError):
        _m(ai_instructions_route=bad)


def test_blank_route_is_none():
    assert _m(ai_instructions_route="  ").ai_instructions_route is None
