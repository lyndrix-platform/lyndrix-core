"""Allowlist validation for a plugin's ``requirements.txt`` and the environment
pip runs in (SC-03).

A plugin may only name packages from the configured index: a PEP 508 name with
optional extras, version constraints and an environment marker. Everything else
pip's requirements format offers — options (``--index-url``, ``-r``, ``-e``),
URLs / VCS refs / local paths, line continuations and ``${VAR}`` expansion — is
refused. The previous blocklist looked at physical lines, while pip first joins
``\\``-continued lines and expands environment variables, so an option could be
smuggled past it and core secrets could be sent to a foreign index URL.
"""
from __future__ import annotations

import os
import re

_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,98}[A-Za-z0-9])?"
_EXTRAS = rf"\[\s*{_NAME}(?:\s*,\s*{_NAME})*\s*\]"
_VERSION_CLAUSE = r"(?:===|==|~=|!=|<=|>=|<|>)\s*[A-Za-z0-9][A-Za-z0-9.*+!_-]{0,63}"
_SPECIFIERS = rf"{_VERSION_CLAUSE}(?:\s*,\s*{_VERSION_CLAUSE})*"
# Marker expressions (``python_version >= "3.8" and sys_platform != "win32"``):
# identifiers, quoted literals, comparison operators and parentheses only.
_MARKER = r"[A-Za-z0-9_.\"'<>=!~()\s-]{1,200}"

_REQUIREMENT_RE = re.compile(
    rf"^{_NAME}\s*(?:{_EXTRAS})?\s*(?:{_SPECIFIERS})?\s*(?:;\s*{_MARKER})?$"
)
# A whitespace-separated token starting with "-" (pip option), also unquoted
# inside a marker.
_OPTION_TOKEN_RE = re.compile(r"(?:^|\s)-")
# pip treats "#" as a comment start at the beginning of a line or after whitespace.
_COMMENT_RE = re.compile(r"(^|\s+)#.*$")

MAX_REQUIREMENT_LINES = 200


def check_requirements(text: str) -> list[str]:
    """Return one message per line that is not a plain requirement specifier.

    An empty list means every line is safe to hand to ``pip install -r``.
    """
    problems: list[str] = []
    lines = text.splitlines()
    if len(lines) > MAX_REQUIREMENT_LINES:
        return [f"more than {MAX_REQUIREMENT_LINES} lines"]

    for number, raw in enumerate(lines, start=1):
        # Checked on the raw line, comments included: pip joins continuation
        # lines before it strips comments, and expands ${VAR} afterwards.
        if "\\" in raw:
            problems.append(f"line {number}: backslash / line continuation is not allowed")
            continue
        if "$" in raw:
            problems.append(f"line {number}: environment variable expansion is not allowed")
            continue
        line = _COMMENT_RE.sub("", raw).strip()
        if not line:
            continue
        # pip splits a line at its first token that starts with "-" and reads
        # everything from there as per-requirement options
        # (--config-settings, --global-option, -i …). The marker alphabet
        # allows "-" and spaces, so such a token must be refused explicitly.
        if _OPTION_TOKEN_RE.search(line):
            problems.append(f"line {number}: pip options are not allowed")
            continue
        if not _REQUIREMENT_RE.fullmatch(line):
            problems.append(
                f"line {number}: only 'name[extras] <version constraints> ; <marker>' is "
                f"allowed (no options, URLs, VCS references or paths)"
            )
    return problems


# What pip (and any sdist build backend it starts) may see. The core's own
# environment carries DB_PASSWORD, LYNDRIX_MASTER_KEY, provider tokens and the
# system API key — none of that belongs in a dependency installer.
_PASSTHROUGH_ENV = (
    "PATH", "HOME", "LANG", "LC_ALL", "TMPDIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "no_proxy", "all_proxy",
    # Custom CA bundles for TLS-intercepting proxies / internal indexes.
    "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
)


def pip_environment(source: dict[str, str] | None = None) -> dict[str, str]:
    """A minimal, secret-free environment for the pip subprocess."""
    source = os.environ if source is None else source
    env = {name: source[name] for name in _PASSTHROUGH_ENV if source.get(name)}
    env.setdefault("PATH", os.defpath)
    env.setdefault("HOME", "/tmp")
    # Never prompt, never phone home for a version check.
    env["PIP_NO_INPUT"] = "1"
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    return env
