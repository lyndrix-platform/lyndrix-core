"""SC-07 + SC-10: provider tokens are bound to their host, never leave it on a
redirect or over http; repository URLs and versions are validated before they
are interpolated into provider URLs."""
import asyncio
import pathlib
import sys

import httpx
import pytest
from fastapi import BackgroundTasks, HTTPException

import core.services  # noqa: F401  (breaks the core.api import cycle)
from core.components.plugins.api import plugins_api
from core.components.plugins.logic import custom_repo_service as custom_repo_module
from core.components.plugins.logic import git_providers
from core.components.plugins.logic import plugin_service as plugin_service_module

sys.path.insert(0, str(pathlib.Path(__file__).parent))  # tests are not a package
from install_fakes import FakePip, install_provider, plugin_archive, service_on  # noqa: E402

FAKE_TOKEN = "dummy-token-not-real"
GITLAB_A = "gitlab.a.invalid"
GITLAB_B = "gitlab.b.invalid"


# --- URL grammar (SC-10) -----------------------------------------------------

@pytest.mark.parametrize("url,host,namespace,repo,provider", [
    ("https://github.com/lyndrix-platform/lyndrix-plugin-itsm", "github.com", "lyndrix-platform", "lyndrix-plugin-itsm", "github"),
    ("https://github.com/lyndrix-platform/lyndrix-plugin-itsm.git", "github.com", "lyndrix-platform", "lyndrix-plugin-itsm", "github"),
    ("https://github.com/org/repo/", "github.com", "org", "repo", "github"),
    ("  https://github.com/org/repo  ", "github.com", "org", "repo", "github"),
    (f"https://{GITLAB_A}/group/sub.group/my_repo", GITLAB_A, "group/sub.group", "my_repo", "gitlab"),
    (f"https://{GITLAB_A}:8443/group/repo", f"{GITLAB_A}:8443", "group", "repo", "gitlab"),
    (f"http://{GITLAB_A}/group/repo", GITLAB_A, "group", "repo", "gitlab"),
    ("git@github.com:org/repo.git", "github.com", "org", "repo", "github"),
    (f"git@{GITLAB_A}:group/sub/repo", GITLAB_A, "group/sub", "repo", "gitlab"),
])
def test_valid_repository_urls_still_parse(url, host, namespace, repo, provider):
    ref = git_providers.parse_repo(url)
    assert (ref.host, ref.namespace, ref.repo, ref.provider) == (host, namespace, repo, provider)
    assert git_providers.detect_provider(url) == provider


@pytest.mark.parametrize("url", [
    f"https://user:{FAKE_TOKEN}@{GITLAB_A}/g/r",
    f"https://{FAKE_TOKEN}@github.com/o/r",
    "https://github.com/o/r?ref=x",
    "https://github.com/o/r#frag",
    "https://github.com/o/..",
    "https://github.com/o/.",
    "https://github.com/../../etc/passwd",
    "https://github.com/o/r/../../x/y",
    "https://github.com/o/re po",
    "https://github.com/o/r\nx",
    "https://github.com/o/r\\x",
    "https://github.com/o/-rf",
    "--upload-pack=x",
    "-oProxyCommand=x@host:o/r",
    f"git@{GITLAB_A}:../../r",
    f"us er@{GITLAB_A}:g/r",
    f"a:b@{GITLAB_A}:g/r",
    "https:///o/r",
    "https://github.com/",
    "",
])
def test_unsafe_repository_urls_are_rejected(url):
    with pytest.raises(ValueError):
        git_providers.parse_repo(url)


def test_rejection_message_does_not_echo_credentials():
    with pytest.raises(ValueError) as exc:
        git_providers.parse_repo(f"https://user:{FAKE_TOKEN}@{GITLAB_A}/g/r")
    assert FAKE_TOKEN not in str(exc.value)


@pytest.mark.parametrize("ref_name", [
    "latest", "v1.2.3", "1.2.3", "1.11.1-rc.1", "2.0.0+build.5", "main", "master",
    "feature/new-ui", "release/1.x", "fix/core_security", "a" * 40,
])
def test_valid_versions(ref_name):
    assert git_providers.validate_ref_name(ref_name) == ref_name


@pytest.mark.parametrize("ref_name", [
    "", "-rf", "--upload-pack=x", "../../../../attacker/evil/archive/refs/tags/v1",
    "x/../../api/v4/users?", "a..b", "v1 .2", "v1\n", "v1\x00", "a//b", "a/", "/a", "a/.hidden",
    "v1?x=1", "v1#x", "v1%2e%2e", "v1@{0}", "a\\b", "x" * 201, None, 5,
])
def test_invalid_versions(ref_name):
    with pytest.raises(ValueError):
        git_providers.validate_ref_name(ref_name)


def test_archive_urls_quote_the_ref_and_refuse_traversal():
    gh = git_providers.parse_repo("https://github.com/org/repo")
    gl = git_providers.parse_repo(f"https://{GITLAB_A}/group/repo")
    assert git_providers.archive_url(gh, "v1.2.3", True) == "https://github.com/org/repo/archive/refs/tags/v1.2.3.zip"
    assert git_providers.archive_url(gh, "feature/x", False) == "https://github.com/org/repo/archive/refs/heads/feature/x.zip"
    assert git_providers.archive_url(gh, "2.0.0+b1", True).endswith("/refs/tags/2.0.0%2Bb1.zip")
    assert git_providers.archive_url(gl, "v1.2.3", True) == f"https://{GITLAB_A}/group/repo/-/archive/v1.2.3/repo-v1.2.3.zip"
    assert git_providers.archive_url(gl, "feature/x", False) == f"https://{GITLAB_A}/group/repo/-/archive/feature%2Fx/repo-feature-x.zip"
    for provider_ref in (gh, gl):
        with pytest.raises(ValueError):
            git_providers.archive_url(provider_ref, "../../../other/repo/archive/refs/tags/v1", True)


# --- token binding (SC-07) ---------------------------------------------------

def _service(tmp_path, monkeypatch, hosts, gitlab_token=FAKE_TOKEN, token_host=""):
    svc = service_on(tmp_path, monkeypatch, FakePip())
    monkeypatch.setattr(plugin_service_module.settings, "LYNDRIX_PLUGIN_REPO_HOSTS", hosts, raising=False)
    monkeypatch.setattr(plugin_service_module.settings, "LYNDRIX_PLUGIN_GITLAB_TOKEN_HOST", token_host, raising=False)
    if gitlab_token:
        monkeypatch.setenv("GITLAB_TOKEN", gitlab_token)
    return svc


def _record(monkeypatch, responder=None):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if responder:
            return responder(request)
        return httpx.Response(200, json=[{"name": "v1.0.0"}])

    install_provider(monkeypatch, b"", handler=handler)
    return seen


def test_gitlab_token_is_not_sent_to_github_for_scp_urls(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch, f"github.com,{GITLAB_A}")
    seen = _record(monkeypatch)

    asyncio.run(svc.get_plugin_versions("git@github.com:owner/repo", force_refresh=True))

    assert [r.url.host for r in seen] == ["api.github.com"]
    assert "private-token" not in seen[0].headers
    assert FAKE_TOKEN not in repr(seen[0].headers)


def test_single_gitlab_host_keeps_working_without_extra_config(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch, f"github.com,{GITLAB_A}")
    seen = _record(monkeypatch)

    tags = asyncio.run(svc.get_plugin_versions(f"https://{GITLAB_A}/g/r", force_refresh=True))

    assert tags == ["v1.0.0"]
    assert seen[0].url.host == GITLAB_A and seen[0].headers["private-token"] == FAKE_TOKEN


def test_token_is_not_guessed_when_several_gitlab_hosts_are_allowed(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch, f"github.com,{GITLAB_A},{GITLAB_B}")
    seen = _record(monkeypatch)

    for host in (GITLAB_A, GITLAB_B):
        asyncio.run(svc.get_plugin_versions(f"https://{host}/g/r", force_refresh=True))

    assert len(seen) == 2 and all("private-token" not in r.headers for r in seen)


def test_token_goes_only_to_the_configured_host(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch, f"github.com,{GITLAB_A},{GITLAB_B}", token_host=GITLAB_B)
    seen = _record(monkeypatch)

    for host in (GITLAB_A, GITLAB_B):
        asyncio.run(svc.get_plugin_versions(f"https://{host}/g/r", force_refresh=True))

    by_host = {r.url.host: r.headers.get("private-token") for r in seen}
    assert by_host == {GITLAB_A: None, GITLAB_B: FAKE_TOKEN}


def test_token_is_never_sent_over_plain_http(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch, GITLAB_A)
    seen = _record(monkeypatch)

    assert asyncio.run(svc.get_plugin_versions(f"http://{GITLAB_A}/g/r", force_refresh=True)) == []
    assert asyncio.run(svc.install_plugin(f"http://{GITLAB_A}/g/sample-plugin")) is False
    assert seen == []  # refused before any request

    with pytest.raises(ValueError, match="https"):
        svc._request_headers(f"http://{GITLAB_A}/g/r", git_providers.parse_repo(f"http://{GITLAB_A}/g/r"))


def test_anonymous_http_repositories_still_work(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch, GITLAB_A, gitlab_token=None)
    seen = _record(monkeypatch)

    assert asyncio.run(svc.get_plugin_versions(f"http://{GITLAB_A}/g/r", force_refresh=True)) == ["v1.0.0"]
    assert len(seen) == 1


# --- redirects ---------------------------------------------------------------

def _gitlab_responder(archive_response):
    def responder(request: httpx.Request) -> httpx.Response:
        if "/api/v4/" in request.url.path:
            return httpx.Response(200, json={"default_branch": "main"})
        return archive_response(request)
    return responder


def test_cross_host_redirect_is_refused_and_the_token_stays_home(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch, GITLAB_A)
    seen = _record(monkeypatch, _gitlab_responder(
        lambda request: httpx.Response(302, headers={"location": "https://evil.invalid/steal.zip"})
    ))

    assert asyncio.run(svc.install_plugin(f"https://{GITLAB_A}/g/sample-plugin")) is False

    assert all(r.url.host == GITLAB_A for r in seen)          # evil.invalid never contacted
    assert not (svc.plugin_dir / "sample_plugin").exists()


def test_same_host_redirect_is_followed_with_the_token(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch, GITLAB_A)
    archive = plugin_archive(requirements=None)

    def archive_response(request):
        if request.url.path.endswith("/moved.zip"):
            return httpx.Response(200, content=archive)
        return httpx.Response(302, headers={"location": "/g/sample-plugin/-/moved.zip"})

    seen = _record(monkeypatch, _gitlab_responder(archive_response))

    assert asyncio.run(svc.install_plugin(f"https://{GITLAB_A}/g/sample-plugin")) is True
    assert seen[-1].url.path.endswith("/moved.zip")
    assert seen[-1].headers["private-token"] == FAKE_TOKEN


def test_github_codeload_redirect_is_followed_without_the_token(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch, "github.com", gitlab_token=None)
    monkeypatch.setenv("GITHUB_TOKEN", FAKE_TOKEN)
    archive = plugin_archive(requirements=None)

    def responder(request):
        if request.url.host == "api.github.com":
            return httpx.Response(200, json={"default_branch": "main"})
        if request.url.host == "github.com":
            return httpx.Response(302, headers={"location": "https://codeload.github.com/o/sample-plugin/zip/refs/tags/v1.2.3"})
        return httpx.Response(200, content=archive)

    seen = _record(monkeypatch, responder)

    assert asyncio.run(svc.install_plugin("https://github.com/o/sample-plugin", version="v1.2.3")) is True
    by_host = {r.url.host: r.headers.get("authorization") for r in seen}
    assert by_host["api.github.com"] == f"token {FAKE_TOKEN}"
    assert by_host["github.com"] == f"token {FAKE_TOKEN}"
    assert by_host["codeload.github.com"] is None


# --- version / custom repositories (SC-10) -----------------------------------

def test_install_rejects_a_traversal_version_before_any_request(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch, "github.com", gitlab_token=None)
    seen = _record(monkeypatch)

    ok = asyncio.run(svc.install_plugin(
        "https://github.com/o/sample-plugin", version="../../../../attacker/evil/archive/refs/tags/v1"))

    assert ok is False and seen == []


def test_branch_names_with_slash_still_install(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch, "github.com", gitlab_token=None)
    archive = plugin_archive(requirements=None)
    seen = _record(monkeypatch, lambda request: (
        httpx.Response(200, json={"default_branch": "main"}) if request.url.host == "api.github.com"
        else httpx.Response(200, content=archive)))

    assert asyncio.run(svc.install_plugin("https://github.com/o/sample-plugin", version="feature/new-ui")) is True
    assert seen[-1].url.path == "/o/sample-plugin/archive/refs/tags/feature/new-ui.zip"


def test_api_rejects_bad_version_and_credential_urls_with_422(tmp_path, monkeypatch):
    svc = _service(tmp_path, monkeypatch, "github.com", gitlab_token=None)
    monkeypatch.setattr(plugins_api, "_plugin_service", lambda: svc)

    class _Identity:
        username = "tester"

    for url, version in (
        ("https://github.com/o/r", "../../x"),
        (f"https://u:{FAKE_TOKEN}@github.com/o/r", "latest"),
        ("https://github.com/o/..", "latest"),
    ):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(plugins_api.install_plugin(
                plugins_api.InstallRequest(url=url, version=version), BackgroundTasks(), _Identity()))
        assert exc.value.status_code == 422
        assert FAKE_TOKEN not in str(exc.value.detail)


def test_custom_repository_must_be_on_the_host_allowlist(tmp_path, monkeypatch):
    _service(tmp_path, monkeypatch, "github.com", gitlab_token=None)
    monkeypatch.setattr(
        plugin_service_module.plugin_service, "_allowed_repo_hosts", lambda: ["github.com"])
    service = custom_repo_module.CustomRepositoryService()
    monkeypatch.setattr(custom_repo_module.db_instance, "SessionLocal", object(), raising=False)
    monkeypatch.setattr(service, "ensure_schema", lambda: None)

    with pytest.raises(ValueError, match="not in the allowed plugin repo hosts"):
        service.create("https://evil.invalid/o/r")
    with pytest.raises(ValueError, match="credentials"):
        service.create(f"https://u:{FAKE_TOKEN}@github.com/o/r")
