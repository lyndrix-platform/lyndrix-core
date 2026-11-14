"""SC-02: a theme is data — its values must never become markup or script.

Covers the strict upload validation (422), the resolve-time fallback for packs
already on disk, and that every built-in theme still validates."""
import asyncio
import io
import json
import pathlib
import zipfile

import pytest
from fastapi import HTTPException

import core.services  # noqa: F401  (breaks the core.api import cycle)
from core.components.settings.api import themes_api
from core.theming import schema
from core.theming.engine import THEMES_BASE_DIR, ThemeEngine
from core.theming.loader import load_theme_pack

XSS = '#000" /><script>alert(1)</script><x a="'
BUILTIN_THEMES = sorted(p.parent.name for p in THEMES_BASE_DIR.glob("*/tokens.json"))


# --- value grammar -----------------------------------------------------------

@pytest.mark.parametrize("value", [
    "#fff", "#ffff", "#0a0e1a", "#0a0e1aff", "rgba(15, 22, 41, 0.58)", "rgb(0 0 0 / 0.55)",
    "hsl(210, 40%, 96%)", "oklch(0.7 0.1 200)", "transparent", "currentColor", "rebeccapurple",
])
def test_valid_colours(value):
    assert schema.is_valid_css_color(value)


@pytest.mark.parametrize("value", [
    "", "#12", "#12345", XSS, "red;background:url(//x)", "url(//evil.example/x)",
    "var(--lx-bg)", "rgb(0,0,0);}", "expression(alert(1))", "#fff\n;x", "rgb(</style>)", None, 7,
])
def test_invalid_colours(value):
    assert not schema.is_valid_css_color(value)


@pytest.mark.parametrize("value", [
    # copies of real values from the shipped/default and user themes
    "'Lyndrix System Sans', 'JetBrains Mono', system-ui, -apple-system, 'Segoe UI', sans-serif",
    'system-ui, -apple-system, "Segoe UI", Roboto, sans-serif',
    "calc(0.6875rem * var(--lx-text-scale))",
    "0 8px 24px -12px rgb(0 0 0 / 0.45)",
    "cubic-bezier(0.4, 0, 0.2, 1)",
    "linear-gradient(135deg, var(--lx-accent), var(--lx-accent-2), var(--lx-accent-3))",
    "160%",
])
def test_safe_free_form_values(value):
    assert schema.is_safe_css_value(value)


@pytest.mark.parametrize("value", [
    "", "red; --x: 1", "a} body{display:none", "</style><script>alert(1)</script>",
    "url(https://evil.example/t.png)", "URL (//evil.example)", "expression(alert(1))",
    "@import 'x'", "red /* c */", "\\3c script", "'unbalanced", "x" * 1001, None,
])
def test_unsafe_free_form_values(value):
    assert not schema.is_safe_css_value(value)


@pytest.mark.parametrize("name,ok", [
    ("--lx-accent", True), ("--my_var-2", True), ("lx-accent", False), ("--", False),
    ("--x:red;--y", False), ("--x}</style>", False), ("--" + "a" * 65, False), ("--lx-accent\n", False),
])
def test_css_var_names(name, ok):
    assert schema.is_valid_css_var_name(name) is ok


@pytest.mark.parametrize("name,ok", [
    ("background-dark.webp", True), ("bg_1.PNG", True), ("a.jpeg", True),
    ("../x.png", False), ("a/b.png", False), ('x.png");}</style>', False), ("x.svg", False),
    (".hidden.png", False), ("x.png?y", False), ("a..png", False), ("", False),
])
def test_asset_filenames(name, ok):
    assert schema.is_safe_asset_filename(name) is ok


# --- built-in themes ---------------------------------------------------------

def test_there_are_builtin_themes():
    assert "default" in BUILTIN_THEMES


@pytest.mark.parametrize("theme_id", BUILTIN_THEMES)
def test_builtin_theme_passes_strict_validation(theme_id):
    pack = load_theme_pack(THEMES_BASE_DIR, theme_id)
    assert schema.theme_pack_errors(pack) == []
    # ...and none of its values is replaced by a fallback at resolve time.
    engine = ThemeEngine(user_dir=pathlib.Path("/nonexistent"))
    for dark in (False, True):
        for key, modes in pack.tokens.colors.items():
            assert engine.resolve_color(key, dark, theme_id) == (modes.dark if dark else modes.light)
        css = engine.resolve_css_variables(dark, theme_id)
        bg = pack.tokens.background_images
        if bg and (bg.dark if dark else bg.light):
            assert (bg.dark if dark else bg.light) in css["--lx-bg-image"]


# --- packs already on disk (uploaded before the strict validation) -------------

def _write_pack(root: pathlib.Path, theme_id: str, tokens: dict) -> None:
    directory = root / theme_id
    directory.mkdir(parents=True)
    (directory / "tokens.json").write_text(json.dumps(tokens))
    (directory / "components.json").write_text(json.dumps({"styles": {}}))


EVIL_TOKENS = {
    "colors": {
        "bg_body": {"light": "#ffffff", "dark": XSS},
        "primary": {"light": "red;x:y", "dark": "#00d4ff"},
    },
    "css_variables": {
        "light": {},
        "dark": {
            "--lx-accent": "red;} </style><script>alert(2)</script>",
            "--lx-x} </style><script>alert(3)</script>": "1",
            "--lx-text": "#abcdef",
        },
    },
    "background_images": {"light": None, "dark": 'x.png"); } </style><script>alert(4)</script>'},
}


def test_engine_drops_unsafe_values_of_a_stored_pack(tmp_path):
    _write_pack(tmp_path, "evil", EVIL_TOKENS)
    engine = ThemeEngine(user_dir=tmp_path)

    assert engine.resolve_color("bg_body", True, "evil") is None
    assert engine.resolve_color("bg_body", False, "evil") == "#ffffff"
    assert engine.resolve_runtime_palette(False, "evil")["primary"] == "#6366f1"  # fallback

    css = engine.resolve_css_variables(True, "evil")
    rendered = "".join(f"{k}:{v};" for k, v in css.items())
    assert "<" not in rendered and "script" not in rendered
    assert css["--lx-bg"] == "#0a0e1a"          # default instead of the payload
    assert css["--lx-accent"] == "#00d4ff"      # unsafe override ignored
    assert css["--lx-text"] == "#abcdef"        # safe override still applied
    assert css["--lx-bg-image"] == "none"


def test_loader_reports_unsafe_values_as_warnings(tmp_path):
    _write_pack(tmp_path, "evil", EVIL_TOKENS)
    warnings = schema.validate_theme_pack(load_theme_pack(tmp_path, "evil"))
    joined = "\n".join(warnings)
    assert "colors.bg_body.dark" in joined and "css_variables.dark" in joined
    assert "background_images.dark" in joined
    assert "<script>" not in joined  # the payload itself is not echoed into logs


def test_render_helper_refuses_declaration_breakers():
    from ui import theme as ui_theme

    assert ui_theme._is_style_safe('url("/api/themes/default/assets/bg.webp?v=1-2")')
    assert ui_theme._is_style_safe("rgba(0, 212, 255, 0.2)")
    for bad in ("red;x", "a}b", "</style>", "a\\3c", None):
        assert not ui_theme._is_style_safe(bad)


# --- upload API --------------------------------------------------------------

class _Upload:
    def __init__(self, filename: str, data: bytes):
        self.filename = filename
        self._data = data

    async def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            data, self._data = self._data, b""
        else:
            data, self._data = self._data[:size], self._data[size:]
        return data


class _Identity:
    username = "tester"


def _zip(tokens, components=None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("tokens.json", tokens if isinstance(tokens, str) else json.dumps(tokens))
        zf.writestr("components.json", json.dumps(components or {"styles": {}}))
    return buffer.getvalue()


@pytest.fixture
def upload(tmp_path, monkeypatch):
    engine = ThemeEngine(user_dir=tmp_path)
    monkeypatch.setattr(themes_api, "_engine", lambda: engine)
    monkeypatch.setattr("core.theming.get_theme_engine", lambda: engine)

    def _do(tokens, name="mytheme.zip"):
        return asyncio.run(themes_api.upload_theme(file=_Upload(name, _zip(tokens)), identity=_Identity()))

    _do.user_dir = tmp_path
    return _do


@pytest.mark.parametrize("tokens,needle", [
    ({"colors": {"bg_body": {"light": "#fff", "dark": XSS}}}, "colors.bg_body.dark"),
    ({"css_variables": {"dark": {"--lx-bg": "red;}</style><script>1</script>"}}}, "css_variables.dark.--lx-bg"),
    ({"css_variables": {"light": {"x}</style>": "1"}}}, "css_variables.light key"),
    ({"background_images": {"dark": 'a.png");}</style>'}}, "background_images.dark"),
    ({"background_images": {"light": "../../secret.png"}}, "background_images.light"),
])
def test_upload_rejects_unsafe_pack_with_422_and_writes_nothing(upload, tokens, needle):
    with pytest.raises(HTTPException) as exc:
        upload(tokens)
    assert exc.value.status_code == 422
    assert needle in exc.value.detail
    assert "<script>" not in exc.value.detail
    assert not (upload.user_dir / "mytheme").exists()


def test_upload_rejects_malformed_json_with_422(upload):
    with pytest.raises(HTTPException) as exc:
        upload("{not json")
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        upload({"colors": {"bg_body": "#fff"}})  # wrong shape
    assert exc.value.status_code == 422


def test_rejected_upload_keeps_the_existing_theme(upload):
    good = {"colors": {"bg_body": {"light": "#ffffff", "dark": "#000000"}}}
    assert upload(good)["status"] == "ok"
    with pytest.raises(HTTPException):
        upload({"colors": {"bg_body": {"light": "#fff", "dark": XSS}}})
    stored = json.loads((upload.user_dir / "mytheme" / "tokens.json").read_text())
    assert stored == good


@pytest.mark.parametrize("theme_id", BUILTIN_THEMES)
def test_builtin_theme_can_be_uploaded_as_is(upload, theme_id):
    tokens = json.loads((THEMES_BASE_DIR / theme_id / "tokens.json").read_text())
    result = upload(tokens, name="copy_of_theme.zip")
    assert result["status"] == "ok" and result["theme_id"] == "copy_of_theme"
    assert (upload.user_dir / "copy_of_theme" / "tokens.json").exists()


# --- one upload path for REST and NiceGUI ------------------------------------

def test_shared_upload_path_writes_to_shared_storage_and_refuses_default(tmp_path, monkeypatch):
    from core.components.settings.logic import themes_service

    engine = ThemeEngine(user_dir=tmp_path / "storage-themes")
    monkeypatch.setattr("core.theming.get_theme_engine", lambda: engine)
    builtin_before = sorted(p.name for p in THEMES_BASE_DIR.iterdir())
    good = _zip({"colors": {"bg_body": {"light": "#ffffff", "dark": "#000000"}}})

    theme_id, _warnings = themes_service.install_theme_zip("nice.zip", good)

    assert theme_id == "nice"
    assert (tmp_path / "storage-themes" / "nice" / "tokens.json").exists()
    assert sorted(p.name for p in THEMES_BASE_DIR.iterdir()) == builtin_before  # image dir untouched

    for filename in ("default.zip", "..zip", "a b.zip", "x/../default.zip", ".zip", "evil;.zip"):
        with pytest.raises(themes_service.ThemeRejected):
            themes_service.install_theme_zip(filename, good)
    with pytest.raises(themes_service.ThemeRejected) as exc:
        themes_service.install_theme_zip("default.zip", good)
    assert exc.value.status_code == 400
    with pytest.raises(themes_service.ThemeRejected, match="Invalid ZIP"):
        themes_service.install_theme_zip("broken.zip", b"not a zip")


def test_nicegui_settings_page_uses_the_shared_upload_path():
    source = (pathlib.Path(themes_api.__file__).parents[1] / "ui" / "settings_ui.py").read_text(encoding="utf-8")
    assert "themes_service.install_theme_zip(" in source
    assert "THEMES_BASE_DIR" not in source  # never writes into the built-in themes of the image
