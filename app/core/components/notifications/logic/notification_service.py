from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from collections import deque
from typing import Any

from nicegui import ui
from nicegui.client import Client

from config import settings
from core.bus import bus
from core.logger import get_logger

log = get_logger("Core:NotificationService")

_HISTORY_LIMIT = 500
_SAVE_DEBOUNCE_SECONDS = 0.5


class NotificationService:
    """Platform notification store: in-memory cache in front of the shared
    database (core 1.8), file-backed until the database is connected.

    Cluster: every instance writes through to the ``notifications`` /
    ``notification_user_states`` tables and re-reads them on access, so the
    bell shows the same entries and read/dismiss state on every instance.
    The pre-1.8 ``notifications.json`` is imported once and then only used as
    a fallback while no database is available.

    Read/dismiss state is tracked **per user** so that one viewer acting on a
    broadcast notification (``user_id is None``) never mutates it for everyone:

    - ``_read_by_user``      maps ``user_id`` → set of notification ids read.
    - ``_dismissed_by_user`` maps ``user_id`` → set of notification ids hidden.

    A broadcast is removed from ``history`` only by an explicit system-level
    ``remove_notification`` (e.g. a sticky "ongoing" task clearing itself), never
    by a single user's dismiss/clear action.
    """

    def __init__(self) -> None:
        self.history: deque[dict[str, Any]] = deque(maxlen=_HISTORY_LIMIT)
        self._read_by_user: dict[str, set[str]] = {}
        self._dismissed_by_user: dict[str, set[str]] = {}
        self.ctx = None
        self.storage_file = os.path.join(settings.STORAGE_DIR, "notifications.json")
        self._dirty = False
        self._save_task: asyncio.Task | None = None
        # Shared DB store (core 1.8): pending write-through operations and the
        # time of the last cache refresh from the database.
        self._db_enabled = False
        self._db_pending: list = []
        self._db_last_refresh = 0.0
        self._db_lock = threading.Lock()
        self._load()

    # ------------------------------------------------------------------
    # Shared database store (core 1.8)
    # ------------------------------------------------------------------

    @property
    def db_enabled(self) -> bool:
        return self._db_enabled

    def enable_db(self) -> None:
        """Switch to the shared database store (blocking — call via ``to_thread``).

        Creates the tables (serialized across booting instances), imports the
        legacy file once when the table is still empty, then replaces the
        in-memory cache with the database content.
        """
        from core.components.database.logic.db_service import db_instance
        from ..model.models import NotificationRow, NotificationUserState

        if not (db_instance.is_connected and db_instance.engine and db_instance.SessionLocal):
            return
        try:
            with db_instance.advisory_lock("lyndrix_bootstrap", timeout=120):
                try:
                    NotificationRow.__table__.create(bind=db_instance.engine, checkfirst=True)
                    NotificationUserState.__table__.create(bind=db_instance.engine, checkfirst=True)
                except Exception as exc:
                    if "1050" not in str(exc) and "already exists" not in str(exc).lower():
                        raise
                with db_instance.SessionLocal() as s:
                    empty = s.query(NotificationRow.id).first() is None
                    if empty and self.history:
                        for n in list(self.history):
                            s.merge(NotificationRow(**self._row_values(n)))
                        for user, ids in self._read_by_user.items():
                            for nid in ids:
                                s.merge(NotificationUserState(user_id=user, notification_id=nid, read=True,
                                                              dismissed=nid in self._dismissed_by_user.get(user, set())))
                        for user, ids in self._dismissed_by_user.items():
                            for nid in ids:
                                if nid not in self._read_by_user.get(user, set()):
                                    s.merge(NotificationUserState(user_id=user, notification_id=nid, read=False, dismissed=True))
                        s.commit()
                        log.info("Notifications: imported %d file-backed entries into the shared store.", len(self.history))
        except Exception as exc:
            log.error("Notifications: shared store unavailable, staying file-backed: %s", exc)
            return
        self._db_enabled = True
        self.refresh_from_db(force=True)
        log.info("Notifications: shared database store active.")

    @staticmethod
    def _row_values(n: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": str(n["id"]),
            "timestamp": float(n.get("timestamp") or time.time()),
            "title": str(n.get("title") or "")[:255],
            "message": str(n.get("message") or ""),
            "type": str(n.get("type") or "info")[:32],
            "user_id": n.get("user_id"),
            "required_permission": n.get("required_permission"),
            "source_plugin_id": n.get("source_plugin_id"),
            "endpoint_name": n.get("endpoint_name"),
        }

    def refresh_from_db(self, *, force: bool = False, min_interval: float = 1.0) -> None:
        """Replace the in-memory cache with the shared store (throttled; blocking)."""
        if not self._db_enabled:
            return
        now = time.monotonic()
        if not force and now - self._db_last_refresh < min_interval:
            return
        from core.components.database.logic.db_service import db_instance
        from ..model.models import NotificationRow, NotificationUserState

        try:
            with db_instance.SessionLocal() as s:
                rows = (s.query(NotificationRow).order_by(NotificationRow.timestamp.desc())
                        .limit(_HISTORY_LIMIT).all())
                states = s.query(NotificationUserState).all()
        except Exception as exc:
            log.warning("Notifications: refresh from the shared store failed: %s", exc)
            return
        history = deque(maxlen=_HISTORY_LIMIT)
        for r in rows:
            history.append({
                "id": r.id, "timestamp": r.timestamp, "title": r.title, "message": r.message, "type": r.type,
                "user_id": r.user_id, "required_permission": r.required_permission,
                "source_plugin_id": r.source_plugin_id, "endpoint_name": r.endpoint_name,
            })
        read: dict[str, set[str]] = {}
        dismissed: dict[str, set[str]] = {}
        for st in states:
            if st.read:
                read.setdefault(st.user_id, set()).add(st.notification_id)
            if st.dismissed:
                dismissed.setdefault(st.user_id, set()).add(st.notification_id)
        self.history, self._read_by_user, self._dismissed_by_user = history, read, dismissed
        self._db_last_refresh = now

    def _db_op(self, fn) -> None:
        """Queue a write-through operation; flushed debounced off the loop."""
        with self._db_lock:
            self._db_pending.append(fn)

    def _db_flush(self) -> None:
        with self._db_lock:
            pending, self._db_pending = self._db_pending, []
        if not pending:
            return
        from core.components.database.logic.db_service import db_instance

        try:
            with db_instance.SessionLocal() as s:
                for fn in pending:
                    fn(s)
                s.commit()
        except Exception as exc:
            log.error("Notifications: write-through to the shared store failed: %s", exc)

    def _db_upsert(self, n: dict[str, Any]) -> None:
        from ..model.models import NotificationRow, NotificationUserState

        values = self._row_values(n)

        def _fn(s):
            s.merge(NotificationRow(**values))
            # An update resurfaces the entry: forget the per-user read marks.
            s.query(NotificationUserState).filter(
                NotificationUserState.notification_id == values["id"], NotificationUserState.read.is_(True),
            ).update({"read": False}, synchronize_session=False)
            # Retention: keep the newest _HISTORY_LIMIT rows.
            ids = [r[0] for r in s.query(NotificationRow.id).order_by(NotificationRow.timestamp.desc())
                   .offset(_HISTORY_LIMIT).all()]
            if ids:
                s.query(NotificationUserState).filter(NotificationUserState.notification_id.in_(ids)).delete(
                    synchronize_session=False)
                s.query(NotificationRow).filter(NotificationRow.id.in_(ids)).delete(synchronize_session=False)

        self._db_op(_fn)

    def _db_delete(self, notif_id: str) -> None:
        from ..model.models import NotificationRow, NotificationUserState

        def _fn(s):
            s.query(NotificationUserState).filter(NotificationUserState.notification_id == notif_id).delete(
                synchronize_session=False)
            s.query(NotificationRow).filter(NotificationRow.id == notif_id).delete(synchronize_session=False)

        self._db_op(_fn)

    def _db_user_state(self, user_id: str, notif_ids: list[str], *, read: bool | None = None,
                       dismissed: bool | None = None) -> None:
        from ..model.models import NotificationUserState

        def _fn(s):
            for nid in notif_ids:
                row = s.get(NotificationUserState, {"user_id": user_id, "notification_id": nid})
                if row is None:
                    row = NotificationUserState(user_id=user_id, notification_id=nid, read=False, dismissed=False)
                    s.add(row)
                if read is not None:
                    row.read = read
                if dismissed is not None:
                    row.dismissed = dismissed

        self._db_op(_fn)

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self) -> None:
        if not os.path.exists(self.storage_file):
            return
        try:
            with open(self.storage_file, "r") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Failed to load notifications from %s: %s", self.storage_file, exc)
            return

        if isinstance(data, list):
            # Legacy format: a bare list of notifications (no per-user state).
            self.history = deque(data, maxlen=_HISTORY_LIMIT)
            return

        self.history = deque(data.get("history", []), maxlen=_HISTORY_LIMIT)
        self._read_by_user = {
            user: set(ids) for user, ids in (data.get("read_by_user") or {}).items()
        }
        self._dismissed_by_user = {
            user: set(ids) for user, ids in (data.get("dismissed_by_user") or {}).items()
        }

    def _serialize(self) -> dict[str, Any]:
        """Build a JSON-able snapshot on the event loop (no I/O here).

        Prunes per-user read/dismiss sets to ids still present in history so the
        bookkeeping cannot grow without bound.
        """
        known_ids = {n["id"] for n in self.history}

        def _prune(by_user: dict[str, set[str]]) -> dict[str, list[str]]:
            pruned: dict[str, list[str]] = {}
            for user, ids in by_user.items():
                kept = [i for i in ids if i in known_ids]
                if kept:
                    pruned[user] = kept
            return pruned

        return {
            "history": list(self.history),
            "read_by_user": _prune(self._read_by_user),
            "dismissed_by_user": _prune(self._dismissed_by_user),
        }

    def _write_file(self, data: dict[str, Any]) -> None:
        try:
            os.makedirs(os.path.dirname(self.storage_file), exist_ok=True)
            tmp_file = self.storage_file + ".tmp"
            with open(tmp_file, "w") as f:
                json.dump(data, f)
            os.replace(tmp_file, self.storage_file)
        except OSError as exc:
            log.error("Failed to persist notifications to %s: %s", self.storage_file, exc)

    def _request_save(self) -> None:
        """Flush pending changes off the event loop (debounced): write-through to
        the shared database when enabled, otherwise the JSON file.

        Falls back to a synchronous write when no event loop is running (e.g.
        during construction or in tests).
        """
        self._dirty = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            if self._db_enabled:
                self._db_flush()
            else:
                self._write_file(self._serialize())
            self._dirty = False
            return

        if self._save_task is None or self._save_task.done():
            self._save_task = loop.create_task(self._debounced_flush())

    async def _debounced_flush(self) -> None:
        # Coalesce bursts of notifications into a single write.
        await asyncio.sleep(_SAVE_DEBOUNCE_SECONDS)
        if not self._dirty:
            return
        self._dirty = False
        if self._db_enabled:
            await asyncio.to_thread(self._db_flush)
            return
        snapshot = self._serialize()
        await asyncio.to_thread(self._write_file, snapshot)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def set_context(self, ctx) -> None:
        self.ctx = ctx

    # ------------------------------------------------------------------
    # Event-bus handlers
    # ------------------------------------------------------------------

    async def handle_system_notify(self, payload: dict[str, Any]) -> None:
        self.persist(payload, broadcast=True)

    async def handle_user_notify(self, payload: dict[str, Any]) -> None:
        self.persist(payload, broadcast=False)

    # ------------------------------------------------------------------
    # Public write API
    # ------------------------------------------------------------------

    def persist(
        self,
        payload: dict[str, Any],
        *,
        broadcast: bool,
        emit_outbound: bool | None = None,
    ) -> None:
        """Record (or update/clear) a notification.

        ``emit_outbound`` controls whether ``notification:outbound`` is re-emitted;
        when ``None`` it is read from ``payload['emit_outbound']`` (default True).
        Pass ``emit_outbound=False`` to persist without triggering external routing.
        """
        notif_id = payload.get("id") or str(time.time())
        action = payload.get("action", "upsert")
        do_persist = payload.get("persist", True)

        if action == "clear":
            self.remove_notification(notif_id)
            return

        message = payload.get("message", "System Notification")
        type_ = payload.get("type", "info")
        title = payload.get("title", "System")
        user_id = payload.get("user_id") if not broadcast else None

        existing = next((n for n in self.history if n["id"] == notif_id), None)
        notif: dict[str, Any] = {
            "id": notif_id,
            "timestamp": time.time(),
            "title": title,
            "message": message,
            "type": type_,
            "user_id": user_id,
            # Routing v2: audience gate (permission id or None) + mute key.
            "required_permission": payload.get("required_permission"),
            "source_plugin_id": payload.get("source_plugin_id"),
            "endpoint_name": payload.get("endpoint_name"),
        }

        if do_persist:
            if existing:
                # Update an existing notification in place and bump it to the top.
                existing.update(
                    {
                        "message": message,
                        "type": type_,
                        "title": title,
                        "timestamp": time.time(),
                    }
                )
                self.history.remove(existing)
                self.history.appendleft(existing)
                # An update resurfaces the entry: clear prior per-user read state.
                self._forget_id(notif_id, dismissed=False)
                notif = existing
                if self.ctx:
                    self.ctx.log.info(f"[UPDATE: {title}] {message}")
            else:
                self.history.appendleft(notif)
                if self.ctx:
                    self.ctx.log.info(f"[{title}] {message}")
        elif self.ctx:
            self.ctx.log.info(f"[TRANSIENT: {title}] {message}")

        # Only show a floating toast if explicitly requested and NOT an ongoing background task
        if broadcast and payload.get("toast", True) and type_ != "ongoing":
            self.broadcast_toast(message, type_)

        should_emit = emit_outbound if emit_outbound is not None else payload.get("emit_outbound", True)
        if self.ctx and should_emit:
            self.ctx.emit("notification:outbound", notif)

        if do_persist:
            if self._db_enabled:
                self._db_upsert(notif)
            self._request_save()
            # Notify connected React frontends (notification bell) over SSE.
            # Additive — does not affect the existing outbound/toast behaviour.
            bus.emit(
                "notification:new",
                {
                    "id": notif["id"],
                    "title": notif["title"],
                    "message": notif["message"],
                    "type": notif["type"],
                    "timestamp": notif["timestamp"],
                    "user_id": notif["user_id"],
                },
            )

    @staticmethod
    def _client_allows(required_permission: str) -> bool:
        """Inside a `with client:` block: does THIS session hold the permission?

        Mirrors the API convention (superadmin flag bypasses, otherwise the
        access_service resolution keyed by the session username).
        """
        try:
            from nicegui import app as _app

            roles = list(_app.storage.user.get("roles", []) or [])
            if "superadmin" in roles:
                return True
            username = str(_app.storage.user.get("username", "") or "")
            if not username:
                return False
            from core.components.auth.logic import access_service

            return access_service.username_has_permission(username, required_permission)
        except Exception:
            return False

    def broadcast_toast(
        self, message: str, type_: str, *, required_permission: str | None = None
    ) -> None:
        toast_type = type_ if type_ in ["positive", "negative", "warning", "info"] else "info"
        for client in list(Client.instances.values()):
            try:
                if client.has_socket_connection:
                    with client:
                        if required_permission and not self._client_allows(required_permission):
                            continue
                        ui.notify(
                            message,
                            type=toast_type,
                            position="top-right",
                            multi_line=True,
                            timeout=3.0,
                        )
            except Exception as exc:
                log.debug("broadcast_toast: skipped a client: %s", exc)

    def remove_notification(self, notif_id: str) -> None:
        """System-level hard delete of a notification for *all* users.

        Used for sticky/"ongoing" entries that clear themselves — not for a
        single user's dismiss (use :meth:`dismiss`).
        """
        self.history = deque(
            (n for n in self.history if n["id"] != notif_id), maxlen=_HISTORY_LIMIT
        )
        self._forget_id(notif_id, dismissed=True)
        if self._db_enabled:
            self._db_delete(notif_id)
        self._request_save()

    # ------------------------------------------------------------------
    # Per-user read / dismiss
    # ------------------------------------------------------------------

    def mark_read(self, user_id: str, notif_id: str) -> None:
        self._read_by_user.setdefault(user_id, set()).add(notif_id)
        if self._db_enabled:
            self._db_user_state(user_id, [notif_id], read=True)
        self._request_save()

    def dismiss(self, user_id: str, notif_id: str) -> None:
        """Hide a notification for a single user (broadcasts stay for others)."""
        self._dismissed_by_user.setdefault(user_id, set()).add(notif_id)
        if self._db_enabled:
            self._db_user_state(user_id, [notif_id], dismissed=True)
        self._request_save()

    def clear_for_user(self, user_id: str) -> None:
        """Dismiss every notification currently visible to *user_id*."""
        dismissed = self._dismissed_by_user.setdefault(user_id, set())
        ids = [n["id"] for n in self.visible_for(user_id)]
        dismissed.update(ids)
        if self._db_enabled and ids:
            self._db_user_state(user_id, ids, dismissed=True)
        self._request_save()

    # ------------------------------------------------------------------
    # Per-user read views
    # ------------------------------------------------------------------

    def visible_for(
        self,
        user_id: str,
        *,
        permission_check=None,
        muted_endpoints: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        """Notifications visible to *user_id*: own + broadcast, minus dismissed.

        Routing v2 layers two ORTHOGONAL predicates over the existing
        targeting check (broadcast semantics unchanged):
        - audience gate: entries carrying ``required_permission`` are dropped
          unless ``permission_check(perm)`` passes (pass ``identity.allows``);
        - personal mute: entries whose ``source_plugin_id/endpoint_name`` key
          is in ``muted_endpoints`` are dropped (display preference).

        Each returned dict carries a per-user ``read`` flag. ``history`` is
        newest-first, so the result keeps that order.
        """
        self.refresh_from_db()   # shared store: see what peers recorded (throttled)
        dismissed = self._dismissed_by_user.get(user_id, set())
        read = self._read_by_user.get(user_id, set())
        result: list[dict[str, Any]] = []
        for n in self.history:
            if n.get("user_id") not in (user_id, None):
                continue
            if n["id"] in dismissed:
                continue
            req = n.get("required_permission")
            if req and not (permission_check and permission_check(req)):
                continue
            if muted_endpoints and n.get("source_plugin_id") and n.get("endpoint_name"):
                if f"{n['source_plugin_id']}/{n['endpoint_name']}" in muted_endpoints:
                    continue
            item = dict(n)
            item["read"] = n["id"] in read
            result.append(item)
        return result

    def unread_for_user(self, user_id: str) -> list[dict[str, Any]]:
        return [n for n in self.visible_for(user_id) if not n["read"]]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _forget_id(self, notif_id: str, *, dismissed: bool) -> None:
        """Drop per-user bookkeeping for a notification id."""
        for ids in self._read_by_user.values():
            ids.discard(notif_id)
        if dismissed:
            for ids in self._dismissed_by_user.values():
                ids.discard(notif_id)


notification_service = NotificationService()
