"""GET /api/permissions/catalog ``implies`` (core 1.8): which permissions a
held one already includes — the shell marks them as 'contained in …'."""
from core.api.permissions_api import _implications


def test_global_api_scopes_and_plugin_umbrellas_imply_their_children():
    ids = [
        "api:read", "api:write", "feature:users.view",
        "plugin:lyndrix.plugin.x", "plugin:lyndrix.plugin.x:api:read", "plugin:lyndrix.plugin.x:api:write",
        "plugin:lyndrix.plugin.x:route:/x", "plugin:lyndrix.plugin.y:api:read",
    ]
    imp = _implications(ids)
    assert imp["api:read"] == ["plugin:lyndrix.plugin.x:api:read", "plugin:lyndrix.plugin.y:api:read"]
    assert imp["api:write"] == ["plugin:lyndrix.plugin.x:api:write"]
    assert imp["plugin:lyndrix.plugin.x"] == [
        "plugin:lyndrix.plugin.x:api:read", "plugin:lyndrix.plugin.x:api:write", "plugin:lyndrix.plugin.x:route:/x",
    ]
    assert "feature:users.view" not in imp and "plugin:lyndrix.plugin.y:api:read" not in imp


def test_no_plugins_no_implications():
    assert _implications(["feature:a", "admin:read"]) == {}
