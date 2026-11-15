"""SC-09: the container cleanup needs a literal prefix and a minimum age, and
never removes the core's own container."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import core.services  # noqa: F401  (breaks the core.api import cycle)
from core.components.sockets.api import socket_api
from core.components.sockets.providers import docker_provider
from core.components.sockets.providers.docker_provider import DockerProvider, validate_cleanup_request

OWN_ID = "ab" * 32
OLD = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat().replace("+00:00", "Z")
NEW = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat().replace("+00:00", "Z")


class _FakeDocker(DockerProvider):
    """DockerProvider with the CLI replaced by a table of containers."""

    def __init__(self, containers):
        super().__init__()
        self.containers = containers  # id -> (name, created)
        self.commands = []
        self.removed = []

    @property
    def is_available(self) -> bool:
        return True

    async def _run_docker(self, *args):
        self.commands.append(args)
        if args[0] == "ps":
            return 0, "\n".join(f"{cid}|{name}|Up|1|t" for cid, (name, _) in self.containers.items()), ""
        if args[0] == "inspect":
            return 0, self.containers[args[-1]][1], ""
        if args[0] == "rm":
            self.removed.append(args[-1])
            return 0, "", ""
        return 1, "", "unexpected"


@pytest.mark.parametrize("prefix", ["aac-runner-", "lyndrix_job_", "ci.runner.", "Job-1-"])
def test_literal_prefixes_are_accepted(prefix):
    assert validate_cleanup_request(prefix, 60) == (prefix, 60)


@pytest.mark.parametrize("prefix", ["", " ", ".*", "^", "a", "ab-", "aac-runner", "aac-.*-", "a|b-", "-rf-", None, 5,
                                    "lyndrix core-", "x" * 80 + "-"])
def test_empty_or_pattern_prefixes_are_rejected(prefix):
    with pytest.raises(ValueError, match="prefix"):
        validate_cleanup_request(prefix, 60)


@pytest.mark.parametrize("age", [0, -1, 4, True, "60", None, 1.5])
def test_too_small_or_non_integer_age_is_rejected(age):
    with pytest.raises(ValueError, match="max_age_minutes"):
        validate_cleanup_request("aac-runner-", age)


def test_api_answers_422_and_never_reaches_docker(monkeypatch):
    monkeypatch.setattr(socket_api, "get_docker_provider", lambda: pytest.fail("must not be called"))
    for body in ({"prefix": "", "max_age_minutes": 0}, {"prefix": ".*"}, {"max_age_minutes": 0}):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(socket_api.docker_cleanup(socket_api.CleanupRequest(**body), auth=None))
        assert exc.value.status_code == 422


def test_api_default_request_still_works(monkeypatch):
    provider = _FakeDocker({"c1": ("aac-runner-old", OLD), "c2": ("aac-runner-new", NEW)})
    monkeypatch.setattr(socket_api, "get_docker_provider", lambda: provider)
    result = asyncio.run(socket_api.docker_cleanup(socket_api.CleanupRequest(), auth=None))
    assert result == {"removed": 1, "error": None}
    assert provider.removed == ["c1"]


def test_provider_refuses_unsafe_input_on_its_own():
    # The bus operation docker:cleanup reaches the provider without the route.
    provider = _FakeDocker({"db": ("mariadb", OLD)})
    for prefix, age in (("", 0), (".*", 60), ("aac-runner-", 0)):
        with pytest.raises(ValueError):
            asyncio.run(provider.cleanup_stale(prefix=prefix, max_age_minutes=age))
    assert provider.commands == [] and provider.removed == []


def test_only_literal_prefix_matches_are_removed_and_the_filter_is_escaped():
    provider = _FakeDocker({
        "c1": ("ci.runner.1", OLD),
        "c2": ("cixrunnerx2", OLD),      # would match an unescaped regex
        "c3": ("mariadb", OLD),          # daemon returned something unrelated
    })
    removed, error = asyncio.run(provider.cleanup_stale(prefix="ci.runner.", max_age_minutes=60))
    assert (removed, error) == (1, None)
    assert provider.removed == ["c1"]
    ps = provider.commands[0]
    assert ps[ps.index("--filter") + 1] == r"name=^ci\.runner\."


@pytest.mark.parametrize("hostname,row_id,row_name", [
    (OWN_ID[:12], OWN_ID, "lyndrix-core-1"),       # default hostname = short id
    ("lyndrix-core-1", "ff" * 32, "lyndrix-core-1"),  # custom hostname = container name
])
def test_own_container_is_never_removed(monkeypatch, hostname, row_id, row_name):
    monkeypatch.setattr(docker_provider, "own_container_id", lambda: hostname if row_id != OWN_ID else OWN_ID)
    monkeypatch.setenv("HOSTNAME", hostname)
    provider = _FakeDocker({row_id: (row_name, OLD), "c9": ("lyndrix-core-old", OLD)})

    removed, _ = asyncio.run(provider.cleanup_stale(prefix="lyndrix-core-", max_age_minutes=60))

    assert removed == 1 and provider.removed == ["c9"]
