"""Tests for ``settings_schema`` fields of kind="action" (core API 1.6): manifest
validation and the settings-update coercion that must never treat a button as
a stored value.

Run inside the lyndrix-core runtime:
    pytest tests/plugins/test_settings_actions.py -v
"""
import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from core.components.plugins.api.plugins_api import coerce_plugin_settings
from core.components.plugins.logic.models import PluginSettingField


def test_action_field_requires_a_route_path():
    with pytest.raises(ValidationError):
        PluginSettingField(key="a", label="A", kind="action")
    with pytest.raises(ValidationError):
        PluginSettingField(key="a", label="A", kind="action", action="maintenance/prune")
    f = PluginSettingField(key="a", label="A", kind="action", action="/maintenance/prune", method="post")
    assert f.method == "POST" and f.confirm is False and f.danger is False


def test_action_method_is_restricted():
    with pytest.raises(ValidationError):
        PluginSettingField(key="a", label="A", kind="action", action="/x", method="GET")


def test_value_fields_ignore_action_attributes():
    f = PluginSettingField(key="n", label="N", kind="int", default="3")
    assert f.action is None and f.method == "POST"


def test_coercion_rejects_actions_and_keeps_values():
    schema = [
        PluginSettingField(key="retention", label="R", kind="int", default="7"),
        PluginSettingField(key="token", label="T", kind="secret"),
        PluginSettingField(key="mode", label="M", kind="select", options=["a", "b"]),
        PluginSettingField(key="action_prune", label="P", kind="action", action="/maintenance/prune"),
    ]
    out = coerce_plugin_settings(schema, {"retention": "9", "token": "", "mode": "b"}, {"token": "kept"})
    assert out == {"retention": 9, "token": "kept", "mode": "b"}

    with pytest.raises(HTTPException) as exc:
        coerce_plugin_settings(schema, {"action_prune": "1"}, {})
    assert exc.value.status_code == 400 and "action" in exc.value.detail

    with pytest.raises(HTTPException):
        coerce_plugin_settings(schema, {"mode": "zzz"}, {})
    with pytest.raises(HTTPException):
        coerce_plugin_settings(schema, {"retention": "x"}, {})
    with pytest.raises(HTTPException):
        coerce_plugin_settings(schema, {"unknown": "1"}, {})
