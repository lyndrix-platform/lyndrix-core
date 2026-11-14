"""
Per-user preferences service.

Backs the account-scoped ("My account") layer of Theming v2 Phase 2. Stores a
single namespaced JSON document per user (``UserPreference``) and exposes small,
synchronous helpers to read, deep-merge, and path-edit it.

The store is intentionally generic: the server never interprets the keys. The
React UI (`preferences.ts`) owns the schema, e.g.::

    {
      "theme": {
        "selection": {"react": "<id|null>", "nicegui": "<id|null>"},
        "token_overrides": {"light": {...}, "dark": {...}}
      },
      "language": "de",
      "layout": {...},
      "visibility": {...}
    }

All methods are blocking (sync SQLAlchemy/pymysql). Callers on the FastAPI event
loop MUST run them via ``def`` route handlers (threadpool) or ``asyncio.to_thread``.
"""
from __future__ import annotations

import copy
from datetime import datetime
from typing import Any, Dict

from core.logger import get_logger
from core.components.database.logic.db_service import db_instance
from .models import UserPreference

log = get_logger("Core:PreferencesService")


def _deep_merge(base: Dict[str, Any], patch: Dict[str, Any]) -> Dict[str, Any]:
    """Recursively merge ``patch`` into ``base`` (in place) and return ``base``.

    Dict values are merged key-by-key; any non-dict value (including lists and
    ``None``) replaces the value at that key. A ``None`` value therefore acts as
    an explicit overwrite, not a delete — use ``delete_path`` to remove a key.
    """
    for key, value in patch.items():
        existing = base.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            _deep_merge(existing, value)
        else:
            base[key] = value
    return base


class PreferencesService:
    """CRUD + deep-merge over the per-user preference document."""

    def _ensure_schema(self) -> None:
        """Create the ``user_preferences`` table if it does not exist.

        Checked once per engine (it used to cost an information_schema round
        trip on EVERY write) and under the cluster bootstrap lock.
        """
        from core.components.database.logic.tx import ensure_table_once

        ensure_table_once(UserPreference.__table__)

    @staticmethod
    def _locked_row(session, username: str):
        """The user's row, locked for update until the transaction ends.

        The document is read, changed and written back as a whole. Without the
        row lock two concurrent writes (two tabs, two instances, the shell's
        debounced panel state next to a theme change) both read the old document
        and the second commit silently dropped the first one's keys.
        """
        return (
            session.query(UserPreference)
            .filter(UserPreference.username == username)
            .with_for_update()
            .first()
        )

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, username: str) -> Dict[str, Any]:
        """Return the stored preference dict for ``username`` (empty dict if none)."""
        if db_instance.SessionLocal is None:
            return {}
        with db_instance.SessionLocal() as s:
            rec = (
                s.query(UserPreference)
                .filter(UserPreference.username == username)
                .first()
            )
            if rec is None or rec.data is None:
                return {}
            # Return a copy so callers can't mutate the (already detached) value.
            return copy.deepcopy(dict(rec.data))

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def merge(self, username: str, partial: Dict[str, Any]) -> Dict[str, Any]:
        """Deep-merge ``partial`` into the stored dict, persist, and return the result.

        Creates the row on first write. ``partial`` must be a dict; nested dicts
        are merged recursively, scalars/lists replace.
        """
        if not isinstance(partial, dict):
            raise ValueError("preferences patch must be an object")
        from core.components.database.logic.tx import run_with_retry, write_session

        self._ensure_schema()

        # One attempt = one short READ COMMITTED transaction on a fresh session.
        # Retried on 1020/1205/1213 and on the duplicate-key error of two
        # requests creating the same user's first row (the loser's second pass
        # finds the row and merges into it) — both used to surface as HTTP 500.
        def _merge_once() -> Dict[str, Any]:
            with write_session() as s:
                rec = self._locked_row(s, username)
                current: Dict[str, Any] = copy.deepcopy(dict(rec.data)) if (rec and rec.data) else {}
                merged = _deep_merge(current, copy.deepcopy(partial))
                if rec is None:
                    s.add(UserPreference(username=username, data=merged, updated_at=datetime.utcnow()))
                else:
                    # Reassign so SQLAlchemy detects the JSON column changed.
                    rec.data = merged
                    rec.updated_at = datetime.utcnow()
                s.commit()
                return merged

        merged = run_with_retry(_merge_once, retry_integrity=True)
        log.debug(f"PREFS: merged preferences for '{username}' ({list(partial.keys())}).")
        return merged

    def set_path(self, username: str, dotted_key: str, value: Any) -> Dict[str, Any]:
        """Set a single dotted path (e.g. ``theme.selection.react``) and persist.

        Convenience wrapper over ``merge`` — builds the nested patch then deep-merges.
        """
        patch = self._nest(dotted_key, value)
        return self.merge(username, patch)

    def delete_path(self, username: str, dotted_key: str) -> Dict[str, Any]:
        """Remove a single dotted path from the stored dict and persist.

        No-op (returns the unchanged dict) if the path does not exist. Prunes now-empty
        parent dicts left behind by the removal.
        """
        parts = [p for p in dotted_key.split(".") if p]
        if not parts or db_instance.SessionLocal is None:
            return self.get(username)
        from core.components.database.logic.tx import run_with_retry, write_session

        self._ensure_schema()

        def _delete_once() -> Dict[str, Any]:
            with write_session() as s:
                rec = self._locked_row(s, username)
                if rec is None or not rec.data:
                    return {}
                data: Dict[str, Any] = copy.deepcopy(dict(rec.data))
                self._delete_in(data, parts)
                rec.data = data
                rec.updated_at = datetime.utcnow()
                s.commit()
                return data

        data = run_with_retry(_delete_once)
        log.debug(f"PREFS: deleted path '{dotted_key}' for '{username}'.")
        return data

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _nest(dotted_key: str, value: Any) -> Dict[str, Any]:
        """Turn ``"a.b.c"`` + value into ``{"a": {"b": {"c": value}}}``."""
        parts = [p for p in dotted_key.split(".") if p]
        if not parts:
            raise ValueError("dotted_key must not be empty")
        patch: Dict[str, Any] = {}
        cursor = patch
        for part in parts[:-1]:
            cursor[part] = {}
            cursor = cursor[part]
        cursor[parts[-1]] = value
        return patch

    @staticmethod
    def _delete_in(node: Dict[str, Any], parts: list[str]) -> None:
        """Recursively delete ``parts`` from ``node``, pruning empty parents."""
        head = parts[0]
        if len(parts) == 1:
            node.pop(head, None)
            return
        child = node.get(head)
        if isinstance(child, dict):
            PreferencesService._delete_in(child, parts[1:])
            if not child:  # prune emptied parent
                node.pop(head, None)


preferences_service = PreferencesService()
