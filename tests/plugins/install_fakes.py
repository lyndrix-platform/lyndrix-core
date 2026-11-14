"""Shared fakes for end-to-end ``install_plugin`` tests: a mocked provider
(httpx ``MockTransport``, no network) and a fake pip subprocess."""
import asyncio
import io
import zipfile

import httpx

from core.components.plugins.logic import plugin_service as plugin_service_module
from core.components.plugins.logic.plugin_service import PluginService

REPO_URL = "https://github.com/example-org/sample-plugin"
ENTRYPOINT = (
    "from core.api import ModuleManifest\n"
    'manifest = ModuleManifest(id="lyndrix.plugin.sample", name="Sample", version="{version}", type="PLUGIN")\n'
    "def setup(ctx): ...\n"
)


def plugin_archive(version: str = "2.0.0", requirements: str | None = "deepmerge==1.1.1\n",
                   extra: dict[str, bytes] | None = None) -> bytes:
    """A GitHub-style archive: everything below one ``<repo>-<ref>/`` folder."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        root = "sample-plugin-main/"
        zf.writestr(root + "entrypoint.py", ENTRYPOINT.format(version=version))
        if requirements is not None:
            zf.writestr(root + "requirements.txt", requirements)
        for name, data in (extra or {}).items():
            zf.writestr(root + name, data)
    return buffer.getvalue()


def install_provider(monkeypatch, archive: bytes, handler=None, requests_seen: list | None = None):
    """Route every httpx call of the plugin service to an in-process handler."""

    def default_handler(request: httpx.Request) -> httpx.Response:
        if requests_seen is not None:
            requests_seen.append(request)
        if request.url.host == "api.github.com":
            return httpx.Response(200, json={"default_branch": "main"})
        return httpx.Response(200, content=archive)

    transport = httpx.MockTransport(handler or default_handler)
    real_client = httpx.AsyncClient

    def client_factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(plugin_service_module.httpx, "AsyncClient", client_factory)


class FakePip:
    """Stand-in for the pip subprocess created by ``_install_requirements``."""

    def __init__(self, returncode: int = 0, hang: bool = False, stderr: bytes = b""):
        self.returncode_on_exit = returncode
        self.hang = hang
        self.stderr = stderr
        self.returncode = None
        self.killed = False
        self.waited = False
        self.calls: list[tuple] = []

    async def __call__(self, *argv, **kwargs):
        self.calls.append((argv, kwargs))
        return self

    async def communicate(self):
        if self.hang:
            await asyncio.sleep(3600)
        self.returncode = self.returncode_on_exit
        return b"", self.stderr

    def kill(self):
        self.killed = True

    async def wait(self):
        self.waited = True
        self.returncode = -9
        return self.returncode


def service_on(tmp_path, monkeypatch, pip: FakePip | None = None) -> PluginService:
    """A PluginService whose plugin directory is ``tmp_path/plugins``."""
    svc = PluginService()
    svc.plugin_dir = tmp_path / "plugins"
    svc.plugin_dir.mkdir()
    monkeypatch.setattr(plugin_service_module.settings, "LYNDRIX_PLUGIN_REPO_HOSTS", "github.com", raising=False)
    monkeypatch.setattr(plugin_service_module.settings, "LYNDRIX_PLUGIN_PIP_CACHE_DIR", "", raising=False)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GITLAB_TOKEN", raising=False)
    if pip is not None:
        monkeypatch.setattr(plugin_service_module.asyncio, "create_subprocess_exec", pip)
    return svc
