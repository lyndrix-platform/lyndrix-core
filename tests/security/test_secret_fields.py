"""SC-08: NiceGUI settings pages never prefill a stored secret; blank = keep."""
import ast
import pathlib
from dataclasses import dataclass

import core.services  # noqa: F401  (breaks the core.api import cycle)
from ui import secret_fields

APP = pathlib.Path(__file__).resolve().parents[2] / "app"
FAKE = "dummy-token-not-real"


@dataclass
class _Field:
    key: str
    sensitive: bool = False
    current_value: str | None = None


def test_only_the_configured_flag_survives():
    assert secret_fields.is_configured(FAKE) is True
    assert secret_fields.is_configured("") is False
    assert secret_fields.is_configured("   ") is False
    assert secret_fields.is_configured(None) is False
    for configured in (True, False):
        assert FAKE not in secret_fields.secret_hint(configured)
    assert secret_fields.secret_hint(True) != secret_fields.secret_hint(False)


def test_blank_input_means_keep():
    assert secret_fields.entered_secret("") is None
    assert secret_fields.entered_secret("   ") is None
    assert secret_fields.entered_secret(None) is None
    assert secret_fields.entered_secret(f"  {FAKE} ") == FAKE


def test_provider_fields_prefill_no_secret():
    assert secret_fields.field_prefill(_Field("bot_token", sensitive=True, current_value=FAKE)) == ""
    assert secret_fields.field_prefill(_Field("channel", current_value="#ops")) == "#ops"
    assert secret_fields.field_prefill(_Field("channel")) == ""


def test_provider_save_skips_blank_secrets_only():
    fields = [_Field("bot_token", sensitive=True), _Field("webhook", sensitive=True), _Field("channel")]
    assert secret_fields.values_to_save(fields, {"bot_token": "", "webhook": FAKE, "channel": ""}) == {
        "webhook": FAKE,   # newly entered secret is stored
        "channel": "",     # a non-secret field may be emptied on purpose
    }                      # bot_token left blank -> stored secret kept
    assert secret_fields.values_to_save(fields, {"bot_token": None}) == {"channel": ""}


def _input_calls(path: pathlib.Path):
    """Every ``ui.input(...)`` call in a module, with its keyword arguments as source."""
    source = path.read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "input" and getattr(node.func.value, "id", "") == "ui"):
            yield {kw.arg: ast.get_source_segment(source, kw.value) for kw in node.keywords if kw.arg}


def test_no_password_input_is_prefilled_with_a_stored_value():
    """Guard for the pages named in the finding: a password input's ``value`` is
    either the empty string or goes through the no-secret helpers."""
    pages = [
        APP / "core/components/settings/ui/settings_ui.py",
        APP / "core/components/notification_router/ui/notifications_settings.py",
        APP / "core/components/auth/ui/auth_settings_card.py",
    ]
    checked = 0
    for page in pages:
        for kwargs in _input_calls(page):
            if "password" not in kwargs or kwargs["password"] == "False":
                continue
            checked += 1
            value = kwargs.get("value", "''")
            assert (
                value in ("''", '""', "display")
                or "field_prefill(" in value
                or "spec.sensitive" in value
            ), f"{page.name}: password input prefilled with {value}"
    assert checked >= 5


def test_settings_page_loads_flags_not_secrets():
    source = (APP / "core/components/settings/ui/settings_ui.py").read_text(encoding="utf-8")
    assert "_load_secret_flags" in source
    assert "_secrets['github_token']" in source          # used as a boolean for the hint
    assert "value=current_token" not in source
    assert "value=current_gl_token" not in source
    assert "value=current_api_key" not in source
