"""Git-host provider abstraction for the plugin installer.

The plugin installer historically assumed every plugin lives on github.com. To
support self-hosted GitLab (and keep the door open for other hosts) this module
turns a repository URL into provider-specific API/archive URLs and auth headers,
so :mod:`plugin_service` can stay host-agnostic.

Only the pieces the installer actually needs are implemented:

* repository metadata (to discover the default branch)
* the tag list (for the version picker)
* the source archive (zip) for a branch or tag
* the correct auth header for a personal access token

GitHub uses ``api.github.com`` + ``/archive/refs/...`` + ``Authorization: token``.
GitLab uses ``{host}/api/v4`` + ``/-/archive/...`` + ``PRIVATE-TOKEN``. GitLab
also allows nested groups, so the project is addressed by the URL-encoded
``namespace/repo`` path rather than a single owner segment.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
from urllib.parse import quote, urlsplit

GITHUB = "github"
GITLAB = "gitlab"


def detect_provider(url: str) -> str:
    """Return ``"github"`` for github.com URLs, else ``"gitlab"``.

    Any non-GitHub host (including self-hosted instances such as
    ``gitlab.int.fam-feser.de``) is treated as GitLab, which is the only other
    provider the installer understands.
    """
    host = (urlsplit(url).netloc or "").lower()
    # Strip optional userinfo (user:pass@host) and port.
    if "@" in host:
        host = host.rsplit("@", 1)[1]
    host = host.split(":", 1)[0]
    if host == "github.com" or host.endswith(".github.com"):
        return GITHUB
    return GITLAB


@dataclass(frozen=True)
class RepoRef:
    """A parsed repository reference.

    ``namespace`` is everything between the host and the repo name, which for
    GitLab may contain nested groups (``group/subgroup``). ``repo`` is the final
    path segment with any ``.git`` suffix removed.
    """

    host: str
    scheme: str
    namespace: str
    repo: str
    provider: str

    @property
    def full_path(self) -> str:
        return f"{self.namespace}/{self.repo}" if self.namespace else self.repo

    @property
    def base_url(self) -> str:
        return f"{self.scheme}://{self.host}/{self.full_path}"


def parse_repo(url: str) -> RepoRef:
    """Parse a repository URL into a :class:`RepoRef`.

    Handles both HTTPS URLs and ``scp``-style SSH URLs
    (``git@host:group/repo.git``). Raises ``ValueError`` when no repo name can be
    extracted.
    """
    raw = url.strip()
    provider = detect_provider(raw)

    # scp-style: git@host:group/repo.git -> normalise to a parseable form.
    if "://" not in raw and "@" in raw and ":" in raw:
        userinfo_host, path = raw.split(":", 1)
        host = userinfo_host.rsplit("@", 1)[-1]
        scheme = "https"
        path_parts = [p for p in path.strip("/").split("/") if p]
    else:
        parts = urlsplit(raw)
        scheme = parts.scheme or "https"
        host = parts.netloc.split("@")[-1]
        path_parts = [p for p in parts.path.strip("/").split("/") if p]

    if not path_parts:
        raise ValueError(f"Cannot parse repository path from URL: {url!r}")

    repo = path_parts[-1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    namespace = "/".join(path_parts[:-1])
    if not repo:
        raise ValueError(f"Empty repository name in URL: {url!r}")

    # Normalise host (drop port for display only when default); keep as-is otherwise.
    return RepoRef(host=host, scheme=scheme, namespace=namespace, repo=repo, provider=provider)


def validate_repo_host(ref: RepoRef, allowed_hosts: Iterable[str]) -> None:
    """Enforce the SSRF host allowlist for a parsed repo reference.

    ``detect_provider`` treats ANY non-github.com host as a trusted GitLab, so
    without this guard a caller-supplied repo URL (install/upgrade/version
    picker) could point the installer's authenticated HTTP client at an
    arbitrary host. Call this immediately after :func:`parse_repo`, before any
    HTTP request is made for the reference.

    A plain hostname allowlist is sufficient here — resolving the hostname or
    special-casing IP literals adds nothing: neither would ever match an entry
    in an operator-curated allowlist of real git hostnames, so they are
    rejected implicitly. Raises ``ValueError`` (safe to surface as an API
    400/422) when the scheme isn't http/https or the host isn't allowed.
    """
    if ref.scheme not in ("http", "https"):
        raise ValueError(
            f"Unsupported URL scheme '{ref.scheme}' — only http/https are allowed"
        )
    allowed = {h.strip().lower() for h in allowed_hosts if h and h.strip()}
    if ref.host.lower() not in allowed:
        raise ValueError(
            f"Repository host '{ref.host}' is not in the allowed plugin repo "
            f"hosts ({', '.join(sorted(allowed)) or 'none configured'})"
        )


def _gitlab_project_id(ref: RepoRef) -> str:
    """URL-encoded ``namespace/repo`` identifier for the GitLab API."""
    return quote(ref.full_path, safe="")


def metadata_url(ref: RepoRef) -> str:
    """API endpoint returning repo metadata (used for the default branch)."""
    if ref.provider == GITHUB:
        return f"https://api.github.com/repos/{ref.full_path}"
    return f"{ref.scheme}://{ref.host}/api/v4/projects/{_gitlab_project_id(ref)}"


def tags_url(ref: RepoRef) -> str:
    """API endpoint returning the list of tags."""
    if ref.provider == GITHUB:
        return f"https://api.github.com/repos/{ref.full_path}/tags"
    return (
        f"{ref.scheme}://{ref.host}/api/v4/projects/"
        f"{_gitlab_project_id(ref)}/repository/tags"
    )


def archive_url(ref: RepoRef, ref_name: str, is_tag: bool) -> str:
    """Source archive (zip) URL for a branch or tag.

    ``is_tag`` only matters for GitHub, which uses different path segments for
    heads vs tags; GitLab uses the same ``/-/archive/{ref}`` form for both.
    """
    if ref.provider == GITHUB:
        kind = "tags" if is_tag else "heads"
        return (
            f"https://github.com/{ref.full_path}/archive/refs/{kind}/{ref_name}.zip"
        )
    # GitLab: https://host/ns/repo/-/archive/<ref>/<repo>-<ref>.zip
    return (
        f"{ref.scheme}://{ref.host}/{ref.full_path}"
        f"/-/archive/{quote(ref_name, safe='')}/{ref.repo}-{ref_name}.zip"
    )


def auth_header(provider: str, token: str) -> dict:
    """Return the auth header dict for a personal access token."""
    if provider == GITHUB:
        return {"Authorization": f"token {token}"}
    return {"PRIVATE-TOKEN": token}


def accept_header(provider: str) -> dict:
    """Provider-appropriate ``Accept`` header for API requests."""
    if provider == GITHUB:
        return {"Accept": "application/vnd.github.v3+json"}
    return {"Accept": "application/json"}


def tag_names(provider: str, payload) -> list[str]:
    """Extract tag name strings from a tags API response.

    Both GitHub and GitLab return a list of objects with a ``name`` field.
    """
    if not isinstance(payload, list):
        return []
    return [item["name"] for item in payload if isinstance(item, dict) and "name" in item]
