"""SC-13: plugin archives are size-capped while downloading and before
extraction; theme ZIPs have total / entry size limits."""
import asyncio
import io
import json
import os
import pathlib
import sys
import zipfile

import httpx
import pytest
from fastapi import HTTPException

import core.services  # noqa: F401  (breaks the core.api import cycle)
from core.components.plugins.logic import plugin_service as plugin_service_module
from core.components.settings.api import themes_api
from core.components.settings.logic import themes_service
from core.theming.engine import ThemeEngine

sys.path.insert(0, str(pathlib.Path(__file__).parent))  # tests are not a package
from install_fakes import REPO_URL, FakePip, install_provider, plugin_archive, service_on  # noqa: E402

MB = 1024 * 1024


def _limits(monkeypatch, archive_mb=200, extracted_mb=1024, files=20000):
    for name, value in (("LYNDRIX_PLUGIN_MAX_ARCHIVE_MB", archive_mb),
                        ("LYNDRIX_PLUGIN_MAX_EXTRACTED_MB", extracted_mb),
                        ("LYNDRIX_PLUGIN_MAX_ARCHIVE_FILES", files)):
        monkeypatch.setattr(plugin_service_module.settings, name, value, raising=False)


def _install(svc):
    return asyncio.run(svc.install_plugin(REPO_URL))


def test_default_limits_fit_the_largest_first_party_plugin():
    from config import Settings

    fields = Settings.model_fields
    # Largest plugin repo today (without vendor/node_modules/.git): ~15 MB, ~200 files.
    assert fields["LYNDRIX_PLUGIN_MAX_ARCHIVE_MB"].default >= 100
    assert fields["LYNDRIX_PLUGIN_MAX_EXTRACTED_MB"].default >= 500
    assert fields["LYNDRIX_PLUGIN_MAX_ARCHIVE_FILES"].default >= 5000


def test_realistic_archive_installs_within_the_limits(tmp_path, monkeypatch):
    svc = service_on(tmp_path, monkeypatch, FakePip())
    _limits(monkeypatch)
    bundle = bytes(range(256)) * 4096 * 3  # ~3 MB UI bundle
    install_provider(monkeypatch, plugin_archive(extra={"app/ui/static/ui_bundle.js": bundle}))

    assert _install(svc) is True
    assert (svc.plugin_dir / "sample_plugin" / "app/ui/static/ui_bundle.js").stat().st_size == len(bundle)


def test_download_is_cut_off_at_the_limit_even_without_content_length(tmp_path, monkeypatch):
    svc = service_on(tmp_path, monkeypatch, FakePip())
    _limits(monkeypatch, archive_mb=1)
    served = {"bytes": 0}

    async def endless():
        while served["bytes"] < 50 * MB:      # far beyond the limit
            served["bytes"] += 256 * 1024
            yield b"\0" * (256 * 1024)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.github.com":
            return httpx.Response(200, json={"default_branch": "main"})
        return httpx.Response(200, content=endless())

    install_provider(monkeypatch, b"", handler=handler)

    assert _install(svc) is False
    assert served["bytes"] <= 2 * MB          # stopped reading right after the cap
    assert list(svc.plugin_dir.iterdir()) == []


def test_declared_content_length_over_the_limit_is_refused_up_front(tmp_path, monkeypatch):
    svc = service_on(tmp_path, monkeypatch, FakePip())
    _limits(monkeypatch, archive_mb=1)
    archive = plugin_archive(extra={"blob.bin": os.urandom(2 * MB)})  # incompressible
    assert len(archive) > MB
    install_provider(monkeypatch, archive)

    assert _install(svc) is False
    assert list(svc.plugin_dir.iterdir()) == []


def test_zip_bomb_is_refused_before_extraction(tmp_path, monkeypatch):
    svc = service_on(tmp_path, monkeypatch, FakePip())
    _limits(monkeypatch, extracted_mb=5)
    bomb = plugin_archive(extra={"bomb.bin": b"\0" * (40 * MB)})   # ~40 kB compressed
    assert len(bomb) < MB
    install_provider(monkeypatch, bomb)

    assert _install(svc) is False
    assert list(svc.plugin_dir.iterdir()) == []


def test_too_many_entries_are_refused(tmp_path, monkeypatch):
    svc = service_on(tmp_path, monkeypatch, FakePip())
    _limits(monkeypatch, files=50)
    install_provider(monkeypatch, plugin_archive(extra={f"f/{i}.txt": b"x" for i in range(100)}))

    assert _install(svc) is False


# --- theme ZIPs --------------------------------------------------------------

class _Upload:
    def __init__(self, filename, data):
        self.filename, self._data = filename, data

    async def read(self, size=-1):
        data = self._data if size is None or size < 0 else self._data[:size]
        self._data = self._data[len(data):]
        return data


class _Identity:
    username = "tester"


def _theme_zip(tokens=None, extra=None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("tokens.json", tokens if tokens is not None else json.dumps({"colors": {}}))
        zf.writestr("components.json", json.dumps({"styles": {}}))
        for name, data in (extra or {}).items():
            zf.writestr(name, data)
    return buffer.getvalue()


@pytest.fixture
def upload(tmp_path, monkeypatch):
    engine = ThemeEngine(user_dir=tmp_path)
    monkeypatch.setattr(themes_api, "_engine", lambda: engine)
    monkeypatch.setattr("core.theming.get_theme_engine", lambda: engine)

    def _do(data: bytes):
        return asyncio.run(themes_api.upload_theme(file=_Upload("big.zip", data), identity=_Identity()))

    _do.user_dir = tmp_path
    return _do


def test_theme_with_images_still_uploads(upload):
    result = upload(_theme_zip(extra={"assets/background-dark.webp": b"RIFF" + b"\1" * (2 * MB)}))
    assert result["status"] == "ok"
    assert (upload.user_dir / "big" / "assets" / "background-dark.webp").exists()


def test_oversized_tokens_json_is_rejected_and_nothing_is_written(upload):
    bomb = _theme_zip(tokens=" " * (themes_service.MAX_THEME_JSON_BYTES + 1))  # compresses to a few kB
    assert len(bomb) < 64 * 1024
    with pytest.raises(HTTPException) as exc:
        upload(bomb)
    assert exc.value.status_code == 413 and "tokens.json" in exc.value.detail
    assert not (upload.user_dir / "big").exists()


def test_oversized_upload_is_rejected(upload, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "LYNDRIX_THEME_MAX_ZIP_MB", 1, raising=False)
    with pytest.raises(HTTPException) as exc:
        upload(b"PK" + bytes(range(256)) * 4096 * 2)
    assert exc.value.status_code == 413
    assert not (upload.user_dir / "big").exists()


def test_theme_zip_expanding_beyond_the_limit_is_rejected(upload, monkeypatch):
    from config import settings

    monkeypatch.setattr(settings, "LYNDRIX_THEME_MAX_ZIP_MB", 1, raising=False)
    with pytest.raises(HTTPException) as exc:
        upload(_theme_zip(extra={"assets/a.png": b"\0" * (3 * MB)}))
    assert exc.value.status_code == 413
    assert not (upload.user_dir / "big").exists()


def test_theme_zip_with_too_many_entries_is_rejected(upload):
    with pytest.raises(HTTPException) as exc:
        upload(_theme_zip(extra={f"assets/{i}.png": b"x" for i in range(themes_service.MAX_THEME_ZIP_ENTRIES + 1)}))
    assert exc.value.status_code == 413
