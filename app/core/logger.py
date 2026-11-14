import logging
import os
import sys
import re
import threading
from logging.handlers import RotatingFileHandler
from collections import deque

# Default log path; the actual directory is resolved from settings.LOGS_DIR at
# setup time (see setup_logging) so the configured path is honoured.
LOG_DIR = "/app/logs"
LOG_FILE = os.path.join(LOG_DIR, "lyndrix.log")

IS_DEBUG = os.getenv("LYNDRIX_DEBUG", "false").lower() == "true"
LOG_LEVEL = logging.DEBUG if IS_DEBUG else logging.INFO

# ENTERPRISE FORMATTING
# [time] | [level] | [component (25 chars)] | message
FORMAT_STR = "%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_LEVEL_RANK = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}


class LogRingBuffer:
    """Thread-safe in-memory log store with PER-SOURCE rings.

    The old single global deque(1000) was shared by every logger in the
    process — Core:EventBus alone (one INFO line per bus emit) evicted all
    Plugin:*/Core:* entries within seconds, and iterating the live deque from
    the UI raced concurrent appends from executor threads. Each source now
    keeps its own bounded ring (sources = logger names, a small stable set),
    plus a global ring for the unfiltered view; reads return copies.
    """

    def __init__(self, per_source_maxlen: int = 300, global_maxlen: int = 2000):
        self._lock = threading.Lock()
        self._by_source: dict[str, deque] = {}
        self._all: deque = deque(maxlen=global_maxlen)
        self._per_source_maxlen = per_source_maxlen

    def append(self, name: str, level: str, message: str) -> None:
        entry = (name, level, message)
        with self._lock:
            self._all.append(entry)
            bucket = self._by_source.get(name)
            if bucket is None:
                bucket = deque(maxlen=self._per_source_maxlen)
                self._by_source[name] = bucket
            bucket.append(entry)

    def snapshot(self, source: str | None = None, limit: int = 200, min_level: str | None = None):
        """Copy of the newest entries (optionally per source / min severity)."""
        with self._lock:
            entries = list(self._by_source.get(source, ())) if source else list(self._all)
        if min_level:
            rank = _LEVEL_RANK.get(min_level.upper(), 0)
            entries = [e for e in entries if _LEVEL_RANK.get(e[1], 0) >= rank]
        return entries[-max(1, limit):]

    def sources(self) -> list[str]:
        with self._lock:
            return sorted(self._by_source.keys())


# Singleton ring queried by the NiceGUI log dialog and GET /api/logs.
log_ring = LogRingBuffer()

# Backwards-compat alias: legacy callers iterated this global deque directly.
# It now points at the ring's global buffer — treat as read-only and prefer
# log_ring.snapshot() (copy-on-read, race-free).
log_capture_buffer = log_ring._all

# Keys we NEVER want to see in plaintext in logs or UI.
SENSITIVE_KEYS = {'token', 'password', 'secret', 'secret_value', 'private_key', 'key', 'auth'}

MASK = "********"

# --- Which *names* (dict keys, env-var names, ``name=value`` in text) carry a secret ---
# Strong markers match as substrings: ``github_token``, ``clientSecret``,
# ``LDAP_BIND_PASSWORD``, ``webhook_url`` and ``accesstoken`` are all caught.
_SENSITIVE_MARKERS = ("token", "password", "passwd", "passphrase", "secret", "credential",
                      "apikey", "authorization", "webhook", "cookie")
# "key" and "auth" are ordinary words too (``sort_key``, ``keyboard``, ``auth_type``,
# ``author``), so they only count as whole words next to a qualifier.
_KEY_QUALIFIERS = frozenset({"api", "private", "secret", "access", "master", "signing", "encryption",
                             "unseal", "ssh", "license", "auth", "bridge", "system", "deploy"})
_AUTH_QUALIFIERS = frozenset({"header", "headers", "basic", "bearer", "digest"})
# A marker inside a name that only *describes* a secret is not a secret:
# ``token_count``, ``has_token``, ``secret_ref``, ``password_min_length``, ``max_tokens``.
_METADATA_WORDS = frozenset({
    "count", "counts", "type", "types", "id", "ids", "name", "names", "ref", "refs", "path", "file",
    "len", "length", "ttl", "expiry", "expires", "expired", "exists", "configured", "set", "enabled",
    "required", "hint", "field", "fields", "kind", "source", "at", "policy", "version", "prefix",
    "label", "endpoint", "used", "usage", "limit", "status", "error", "errors", "valid", "invalid",
    "missing", "min", "max", "age", "rotation", "rotated", "created", "updated", "mode",
})
_METADATA_PREFIXES = frozenset({"has", "is", "no", "num", "max", "min", "use", "uses", "with",
                                "without", "require", "requires", "needs", "show", "hide"})
_BENIGN_WORDS = frozenset({"tokenizer", "tokenize", "tokenized", "tokenization"})
_WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z]?[a-z]+|[0-9]+")

# Dict keys whose content is a list of environment variables: every value is masked,
# the names stay readable (``docker:spawn`` requests, runner env, ...).
_ENV_CONTAINERS = {"env_vars", "env", "environment"}
# Environment-variable style names (SCREAMING_SNAKE). Anything else that sits in a
# ``key`` field could itself be a secret (API keys are identifier-like too) and stays masked.
_ENV_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]{0,63}")
_ENV_ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=")

# Bounded memo: the set of names seen in logs is small and stable, and this runs
# for every dict key of every logged bus payload.
_NAME_CACHE: dict[str, bool] = {}
_NAME_CACHE_MAX = 4096


def _classify_name(name: str) -> bool:
    lowered = name.lower()
    if lowered in SENSITIVE_KEYS:
        return True
    words = [w.lower() for w in _WORD_RE.findall(name)]
    if not words:
        return False
    marked = any(marker in lowered for marker in _SENSITIVE_MARKERS)
    if marked and any(w in _BENIGN_WORDS for w in words):
        # "tokenizer_model" & co: the marker is part of an unrelated word.
        remaining = "".join(w for w in words if w not in _BENIGN_WORDS)
        marked = any(marker in remaining for marker in _SENSITIVE_MARKERS)
    if not marked:
        if "key" in words or "keys" in words:
            marked = any(w in _KEY_QUALIFIERS for w in words)
        elif "auth" in words:
            marked = any(w in _AUTH_QUALIFIERS for w in words)
    if not marked:
        return False
    return words[-1] not in _METADATA_WORDS and words[0] not in _METADATA_PREFIXES


def _sensitive_name(name: str) -> bool:
    """True when a key / variable name says its value is a secret."""
    cached = _NAME_CACHE.get(name)
    if cached is None:
        cached = _classify_name(name)
        if len(_NAME_CACHE) < _NAME_CACHE_MAX:
            _NAME_CACHE[name] = cached
    return cached


# --- Secrets inside free text (messages, %-args, exception text, tracebacks) ---
# One cheap pre-check decides whether any of the substitutions can apply at all;
# the vast majority of log lines stop there.
_TEXT_HINT_RE = re.compile(
    r"://|[:=]|bearer|lyk_|ghp_|gh[ousr]_|github_pat_|glpat-|hvs\.|sk-|eyJ", re.IGNORECASE
)
# https://user:secret@host and https://secret@host (``git@`` is a login, not a secret).
_URL_USERINFO_RE = re.compile(r"(?<=://)(?!git@)[^/\s:@\"']+(?::[^/\s@\"']*)?@")
_HEADER_RE = re.compile(
    r"\b((?:proxy-)?authorization|private-token|x-api-key|x-gitlab-token|x-auth-token|(?:set-)?cookie)"
    r"(['\"]?\s*[:=]\s*['\"]?)(?:(bearer|basic|token|digest)\s+)?[^\s'\",]+",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE)
_TOKEN_PREFIX_RE = re.compile(
    r"\b(?:lyk_|ghp_|gho_|ghu_|ghs_|ghr_|github_pat_|glpat-)[A-Za-z0-9_.\-]{6,}"
    r"|\bhvs\.[A-Za-z0-9_\-]{16,}"
    r"|\bsk-[A-Za-z0-9_\-]{16,}"
    # JSON Web Tokens (OIDC id/access tokens, signed tickets): header.payload.signature
    r"|\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"
)
# name=value / name: value / "name": "value" — the callback decides by the name.
_ASSIGNMENT_RE = re.compile(
    r"(?<![A-Za-z0-9_])(['\"]?)([A-Za-z_][A-Za-z0-9_.\-]{1,63})\1(\s*[:=]\s*)"
    r"(\"[^\"\n]*\"|'[^'\n]*'|[^\s'\",;&)\]}]+)"
)
# Bare "key"/"auth" are prose words as well ("AUTH: Token restored", "Key: Value"),
# so in text they only count in the unambiguous forms ``key=…`` and ``"key": …``.
_AMBIGUOUS_TEXT_NAMES = frozenset({"key", "auth"})


def _mask_assignment(match: "re.Match[str]") -> str:
    quote, name, separator, value = match.groups()
    if not _sensitive_name(name):
        return match.group(0)
    if name.lower() in _AMBIGUOUS_TEXT_NAMES and not quote and "=" not in separator:
        return match.group(0)
    if value[:1] in "\"'":
        return f"{quote}{name}{quote}{separator}{value[0]}{MASK}{value[0]}"
    return f"{quote}{name}{quote}{separator}{MASK}"


def mask_text(text: str) -> str:
    """Mask credentials inside a free-form string.

    Covers URL userinfo (``https://user:token@host``), ``Authorization`` /
    ``PRIVATE-TOKEN``-style header values, ``Bearer <token>``, well-known token
    prefixes (``lyk_``, ``ghp_``, ``github_pat_``, ``glpat-``, ``hvs.``, ``sk-``),
    JWTs and ``name=value`` pairs whose name marks a secret.

    Known limit: a secret with no recognisable shape that is logged without a
    name next to it (``log.info("token %s", opaque)``) cannot be told apart
    from ordinary text — log secrets as ``name=%s`` or not at all.
    """
    if not text or not _TEXT_HINT_RE.search(text):
        return text
    if "://" in text:
        text = _URL_USERINFO_RE.sub("***@", text)
    text = _HEADER_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{(m.group(3) + ' ') if m.group(3) else ''}{MASK}", text)
    text = _BEARER_RE.sub(lambda m: f"{m.group(1)} {MASK}", text)
    text = _TOKEN_PREFIX_RE.sub(MASK, text)
    return _ASSIGNMENT_RE.sub(_mask_assignment, text)


def mask_secrets(obj, *, in_env: bool = False):
    """Recursively replace sensitive values in dicts/lists with a mask.

    Reused by both the log formatter and the event bus so payload dicts are
    redacted by key name before they are stringified for logging. ``{key, value}``
    pairs (environment-variable style) always mask the *value*; the *name* stays
    readable only when it looks like an env-var name (``LYNDRIX_API_URL``), so a
    ``docker:spawn`` request shows which variables were set but never a token.
    Inside an environment container (``env``, ``env_vars``, ``environment``) every
    value is masked, whatever its shape. Strings are scrubbed with ``mask_text``.
    """
    if isinstance(obj, dict):
        key_name = obj.get("key")
        if "value" in obj and isinstance(key_name, str):
            name = key_name if _ENV_NAME_RE.fullmatch(key_name) else MASK
            return {**{k: mask_secrets(v) for k, v in obj.items() if k not in ("key", "value")},
                    "key": name, "value": MASK}
        if in_env:
            # {"GITLAB_TOKEN": "…", "API_URL": "…"} — names stay, values never.
            return {k: MASK for k in obj}
        return {
            k: MASK if (isinstance(k, str) and _sensitive_name(k))
            else mask_secrets(v, in_env=isinstance(k, str) and k.lower() in _ENV_CONTAINERS)
            for k, v in obj.items()
        }
    elif isinstance(obj, (list, tuple)):
        return [mask_secrets(i, in_env=in_env) for i in obj]
    elif isinstance(obj, str):
        if in_env:
            # "NAME=value" entries of an env list.
            assignment = _ENV_ASSIGNMENT_RE.match(obj)
            return f"{assignment.group(1)}={MASK}" if assignment else MASK
        return mask_text(obj)
    return obj


class EnterpriseFormatter(logging.Formatter):
    # Kept for backwards compatibility; delegates to the module-level helper.
    SENSITIVE_KEYS = SENSITIVE_KEYS

    def _mask_secrets(self, obj):
        return mask_secrets(obj)

    def format(self, record):
        # Every handler shares this formatter; mask once per record, not per handler.
        cached = record.__dict__.get("_lyndrix_masked")
        if cached is not None and cached[0] is self:
            return cached[1]

        # Mask the FINAL message — after %-args are merged — so
        # ``log.info("token %s", value)`` cannot slip a secret past the masker.
        # Dict messages (event-bus payloads) are redacted by key name first.
        original_msg, original_args = record.msg, record.args
        try:
            if isinstance(record.msg, dict):
                message = str(mask_secrets(record.msg))
            else:
                message = mask_text(record.getMessage())
            record.msg, record.args = message, None
            line = super().format(record)
        finally:
            record.msg, record.args = original_msg, original_args

        record.__dict__["_lyndrix_masked"] = (self, line)
        return line

    # Exception text and stack dumps are appended by logging.Formatter.format();
    # they carry str(exc), which routinely contains URLs and command lines.
    def formatException(self, ei):
        return mask_text(super().formatException(ei))

    def formatStack(self, stack_info):
        return mask_text(super().formatStack(stack_info))

class RingBufferHandler(logging.Handler):
    """Keeps logs in RAM for the UI log viewer / GET /api/logs."""
    def emit(self, record):
        log_entry = self.format(record)
        log_ring.append(record.name, record.levelname, log_entry)

def setup_logging():
    # Resolve the log directory from settings (config hierarchy) and create it
    # here rather than as an import-time side effect.
    log_dir = LOG_DIR
    try:
        from config import settings
        log_dir = settings.LOGS_DIR or LOG_DIR
    except Exception:
        # Settings not importable yet — fall back to the module default.
        pass
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, "lyndrix.log")

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    if root_logger.hasHandlers():
        root_logger.handlers.clear()

    # Formatter instance
    formatter = EnterpriseFormatter(FORMAT_STR, datefmt="%H:%M:%S")

    # 1. STREAM HANDLER (console)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(LOG_LEVEL)
    root_logger.addHandler(console_handler)

    # 2. FILE HANDLER
    file_handler = RotatingFileHandler(log_file, maxBytes=10*1024*1024, backupCount=5, encoding="utf-8")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)
    root_logger.addHandler(file_handler)

    # 3. MEMORY HANDLER (for the UI log viewer)
    memory_handler = RingBufferHandler()
    memory_handler.setFormatter(formatter)
    memory_handler.setLevel(logging.DEBUG)
    root_logger.addHandler(memory_handler)

    # 4. CLUSTER HANDLER — buffers lines for the shared cluster_logs table so the
    # UI can show every instance's log from any instance (shipped once the DB is
    # connected; see core/components/cluster/logic/log_store.py). INFO+ by default.
    try:
        from core.components.cluster.logic.log_store import cluster_log_handler

        cluster_log_handler.setFormatter(formatter)
        cluster_log_handler.setLevel(logging.INFO)
        root_logger.addHandler(cluster_log_handler)
    except Exception as exc:  # pragma: no cover - import order in exotic setups
        logging.getLogger("Core:Logger").debug(f"cluster log handler not attached: {exc}")

    # Quiet down external loggers
    silent_loggers = ["uvicorn", "uvicorn.access", "sqlalchemy.engine", "hvac", "urllib3", "nicegui", "httpx"]
    for name in silent_loggers:
        lg = logging.getLogger(name)
        lg.setLevel(logging.WARNING if not IS_DEBUG else logging.DEBUG)
        lg.propagate = False
        lg.handlers = root_logger.handlers

    logging.info(f"LOGGING: Initialized with level {'DEBUG' if IS_DEBUG else 'INFO'}")

def get_logger(name: str):
    """Factory method for consistent logger names."""
    return logging.getLogger(name)