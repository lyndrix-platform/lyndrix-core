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

import re
from dataclasses import dataclass
from typing import Iterable
from urllib.parse import quote, urlsplit

GITHUB = "github"
GITLAB = "gitlab"


def provider_for_host(host: str) -> str:
    """``"github"`` for github.com (and its subdomains), else ``"gitlab"``."""
    host = (host or "").lower().split(":", 1)[0]
    if host == "github.com" or host.endswith(".github.com"):
        return GITHUB
    return GITLAB


def detect_provider(url: str) -> str:
    """Return ``"github"`` for github.com URLs, else ``"gitlab"``.

    Any non-GitHub host (including self-hosted instances such as
    ``gitlab.int.fam-feser.de``) is treated as GitLab, which is the only other
    provider the installer understands. The host is taken from the *parsed*
    reference, so ``git@github.com:org/repo`` is GitHub too — judging by URL
    shape sent the GitLab token to github.com for scp-style URLs (SC-07).
    """
    try:
        return parse_repo(url).provider
    except ValueError:
        return provider_for_host(urlsplit(url.strip()).hostname or "")


# --- Input grammar (SC-10) ------------------------------------------------------
# Repository coordinates and refs are interpolated into provider API / archive
# URLs. Anything outside these alphabets could redirect the request to another
# repository or API endpoint (dot segments, "?"), or smuggle credentials.
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?(:[0-9]{1,5})?$")
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,99}$")
_SCP_USER_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._-]{0,31}$")
_REF_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._+/-]{0,199}$")
_UNSAFE_URL_CHARS_RE = re.compile(r"[\s\x00-\x1f\x7f\\]")


def validate_ref_name(ref_name: str) -> str:
    """Validate a version / tag / branch name and return it unchanged.

    Accepts ``latest``, tags like ``v1.2.3`` / ``1.2.3`` and branch names with
    ``/``. Rejects traversal (``..``), a leading ``-``, whitespace, control
    characters and URL metacharacters. Raises ``ValueError``.
    """
    name = ref_name if isinstance(ref_name, str) else ""
    if (
        not _REF_NAME_RE.fullmatch(name)
        or ".." in name
        or "//" in name
        or name.endswith(("/", "."))
        or any(part.startswith(".") for part in name.split("/"))
    ):
        raise ValueError(
            "Invalid version/ref: use 'latest', a tag such as 'v1.2.3' or a branch "
            "name (letters, digits, '.', '_', '+', '-', '/'; no '..')"
        )
    return name


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
    (``git@host:group/repo.git``). Raises ``ValueError`` when the URL is not a
    plain repository address: embedded credentials, query strings, fragments,
    traversal segments and whitespace/control characters are refused instead
    of being silently dropped (SC-10) — a URL with a token in it would
    otherwise be logged and echoed back.
    """
    raw = (url or "").strip()
    if not raw or _UNSAFE_URL_CHARS_RE.search(raw) or raw.startswith("-"):
        raise ValueError("Invalid repository URL")

    # scp-style: git@host:group/repo.git -> normalise to a parseable form.
    if "://" not in raw and "@" in raw and ":" in raw:
        userinfo_host, path = raw.split(":", 1)
        user, _, host = userinfo_host.rpartition("@")
        # The login of an scp-style address ("git") is not a credential, but it
        # must not be anything that could carry one.
        if not _SCP_USER_RE.fullmatch(user):
            raise ValueError("Invalid repository URL: unexpected user part")
        scheme = "https"
        raw_path = path
    else:
        parts = urlsplit(raw)
        scheme = (parts.scheme or "https").lower()
        if "@" in parts.netloc:
            raise ValueError(
                "Repository URL must not contain credentials — configure the "
                "access token in the plugin settings instead"
            )
        if parts.query or parts.fragment or "?" in raw or "#" in raw:
            raise ValueError("Repository URL must not contain a query string or fragment")
        host = parts.netloc
        raw_path = parts.path

    if not _HOST_RE.fullmatch(host or ""):
        raise ValueError(f"Invalid repository host in URL: {url!r}")

    path_parts = [p for p in raw_path.strip("/").split("/") if p]
    if not path_parts:
        raise ValueError(f"Cannot parse repository path from URL: {url!r}")

    repo = path_parts[-1]
    if repo.endswith(".git"):
        repo = repo[:-4]
    if not repo:
        raise ValueError(f"Empty repository name in URL: {url!r}")
    # "." / ".." (and anything else outside the alphabet) would become a plugin
    # directory name and a path segment of every provider URL.
    for segment in (*path_parts[:-1], repo):
        if segment in (".", "..") or ".." in segment or not _PATH_SEGMENT_RE.fullmatch(segment):
            raise ValueError(f"Invalid repository path segment in URL: {segment!r}")
    namespace = "/".join(path_parts[:-1])

    return RepoRef(
        host=host, scheme=scheme, namespace=namespace, repo=repo,
        provider=provider_for_host(host),
    )


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
    The ref is validated and URL-quoted: unquoted, ``../`` segments in a
    "version" re-targeted the download to another repository or API path.
    """
    ref_name = validate_ref_name(ref_name)
    if ref.provider == GITHUB:
        kind = "tags" if is_tag else "heads"
        # "/" stays literal: GitHub addresses "feature/x" as nested path segments.
        return (
            f"https://github.com/{ref.full_path}/archive/refs/{kind}/"
            f"{quote(ref_name, safe='/')}.zip"
        )
    # GitLab: https://host/ns/repo/-/archive/<ref>/<repo>-<ref>.zip
    file_ref = quote(ref_name.replace("/", "-"), safe="")
    return (
        f"{ref.scheme}://{ref.host}/{ref.full_path}"
        f"/-/archive/{quote(ref_name, safe='')}/{ref.repo}-{file_ref}.zip"
    )


def token_allowed_for(ref: RepoRef, token_host: str | None) -> bool:
    """True when a token configured for ``token_host`` may be sent for ``ref``.

    A token belongs to exactly one host (SC-07): it is only attached when the
    reference points at that host. GitHub references always talk to
    github.com / api.github.com (hard-coded URLs), so their token host is fixed.
    """
    if not token_host:
        return False
    return ref.host.lower() == token_host.strip().lower()


def same_origin(first: str, second: str) -> bool:
    """True when two absolute URLs share scheme, host and port."""
    a, b = urlsplit(first), urlsplit(second)
    default = {"https": 443, "http": 80}
    return (
        a.scheme == b.scheme
        and (a.hostname or "").lower() == (b.hostname or "").lower()
        and (a.port or default.get(a.scheme)) == (b.port or default.get(b.scheme))
    )


# Hosts a provider is known to redirect archive downloads to. The redirect URL
# is self-authorising (signed), so it is followed WITHOUT any auth header.
ARCHIVE_REDIRECT_HOSTS: dict[str, frozenset[str]] = {
    GITHUB: frozenset({"codeload.github.com"}),
    GITLAB: frozenset(),
}


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
