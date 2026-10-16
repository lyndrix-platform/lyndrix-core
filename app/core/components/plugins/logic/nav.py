"""NavRegistry — sidebar entries a plugin contributes at runtime (core API ≥ 1.9).

A plugin whose "pages" are data — External Services (one entry per embedded
service), a per-site console, a saved dashboard — cannot declare them in its
manifest. It registers them here through ``ctx.register_nav_entries()``; the
React shell reads ``GET /api/nav`` and refetches on ``nav:changed``.

Entries are per process: every instance registers its own set from the shared
database (a cluster event such as ``external_services:changed`` tells the peers
to re-read). They are dropped when the owning module is disposed, so a disabled
plugin never leaves dangling links behind, and they never touch the module
registry — the plugin set (and its cluster fingerprint) stays what is actually
installed.
"""
from __future__ import annotations

import re
import threading
from dataclasses import asdict, dataclass
from typing import Iterable, Optional

from core.bus import bus
from core.logger import get_logger

log = get_logger("Core:Nav")

NAV_CHANGED_TOPIC = "nav:changed"
OPEN_MODES = ("iframe", "new_tab", "route")   # route: a path inside the shell (API 1.10)
DEFAULT_GROUP = "Services"
DEFAULT_ICON = "open_in_browser"
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
_URL_RE = re.compile(r"^(https?://|/)", re.IGNORECASE)


@dataclass(frozen=True)
class NavEntry:
    id: str                       # stable per plugin, URL-safe (slug)
    label: str
    url: str                      # absolute http(s) URL or a path on this origin
    icon: str = DEFAULT_ICON      # Material icon name (mapped by the shell)
    open_mode: str = "iframe"     # iframe (embedded page) | new_tab | route (in-shell path)
    group: str = DEFAULT_GROUP    # sidebar section label
    description: Optional[str] = None
    order: int = 0


def _coerce(plugin_id: str, raw: dict) -> NavEntry:
    if not isinstance(raw, dict):
        raise ValueError("entry must be a dict")
    entry_id = str(raw.get("id") or "").strip()
    if not _ID_RE.match(entry_id):
        raise ValueError(f"invalid entry id {entry_id!r}")
    label = str(raw.get("label") or "").strip()
    if not label:
        raise ValueError(f"entry {entry_id!r} has no label")
    url = str(raw.get("url") or "").strip()
    if not _URL_RE.match(url):
        raise ValueError(f"entry {entry_id!r}: url must be http(s)://… or an absolute path")
    open_mode = str(raw.get("open_mode") or "iframe")
    if open_mode not in OPEN_MODES:
        raise ValueError(f"entry {entry_id!r}: open_mode must be one of {OPEN_MODES}")
    if open_mode == "route" and not url.startswith("/"):
        raise ValueError(f"entry {entry_id!r}: open_mode 'route' needs an in-shell path (e.g. /apps/<plugin>/…)")
    try:
        order = int(raw.get("order") or 0)
    except (TypeError, ValueError):
        order = 0
    return NavEntry(
        id=entry_id,
        label=label[:120],
        url=url[:2000],
        icon=str(raw.get("icon") or DEFAULT_ICON)[:100],
        open_mode=open_mode,
        group=str(raw.get("group") or DEFAULT_GROUP)[:60],
        description=(str(raw["description"])[:500] if raw.get("description") else None),
        order=order,
    )


class NavRegistry:
    """Thread-safe store of ``{plugin_id: (NavEntry, …)}``."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[NavEntry, ...]] = {}
        self._lock = threading.Lock()

    def register(self, plugin_id: str, entries: Iterable[dict]) -> int:
        """Replace ``plugin_id``'s entries; emits ``nav:changed`` when the set changed.

        Invalid entries are skipped with a warning, the valid rest is kept.
        Returns the number of registered entries.
        """
        valid: dict[str, NavEntry] = {}
        for raw in entries or ():
            try:
                entry = _coerce(plugin_id, raw)
            except ValueError as exc:
                log.warning(f"NAV: '{plugin_id}' entry skipped — {exc}")
                continue
            valid[entry.id] = entry  # last wins on a duplicate id
        new = tuple(sorted(valid.values(), key=lambda e: (e.group, e.order, e.label.lower())))
        with self._lock:
            old = self._entries.get(plugin_id, ())
            if new:
                self._entries[plugin_id] = new
            else:
                self._entries.pop(plugin_id, None)
        if new != old:
            log.info(f"NAV: '{plugin_id}' registers {len(new)} sidebar entr{'y' if len(new) == 1 else 'ies'}")
            self._announce(plugin_id, len(new))
        return len(new)

    def clear(self, plugin_id: str) -> None:
        with self._lock:
            had = self._entries.pop(plugin_id, None)
        if had:
            log.info(f"NAV: '{plugin_id}' entries removed ({len(had)})")
            self._announce(plugin_id, 0)

    def entries(self, plugin_ids: Optional[Iterable[str]] = None) -> list[dict]:
        """Flat list of entry dicts (+ ``plugin_id``), optionally limited to some plugins."""
        with self._lock:
            items = list(self._entries.items())
        allowed = set(plugin_ids) if plugin_ids is not None else None
        out: list[dict] = []
        for pid, entries in items:
            if allowed is not None and pid not in allowed:
                continue
            for entry in entries:
                out.append({"plugin_id": pid, **asdict(entry)})
        out.sort(key=lambda e: (e["group"], e["order"], e["label"].lower()))
        return out

    def plugin_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._entries)

    def _announce(self, plugin_id: str, count: int) -> None:
        try:
            bus.emit(NAV_CHANGED_TOPIC, {"plugin_id": plugin_id, "count": count})
        except Exception:  # a broken listener must never break the registering plugin
            log.debug("NAV: nav:changed emit failed", exc_info=True)


nav_registry = NavRegistry()
