"""Boot state machine and the boot dependency snapshot behind ``GET /api/boot``.

The state machine (``waiting_core`` → ``loading_modules`` → ``ready`` | ``failed``)
is unchanged. New is :meth:`BootService.snapshot`, which turns the live state of
every service the core depends on into one list the boot screens (React shell,
NiceGUI) render: what is already online, what is still coming, what needs the
operator (Vault init/unseal) and what failed. In ``LYNDRIX_CLUSTER_MODE=cluster``
the cluster dependencies are listed too, so an operator sees at a glance which
parts of a cluster deployment this core version provides.

The snapshot is cheap by design: it reads in-memory flags only (no Vault or DB
round-trip), so it can be polled every second by every open browser tab while
the platform comes up.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any, Dict, List, Optional

from core.bus import bus
from core.logger import get_logger

log = get_logger("Core:BootService")


class BootPhase(StrEnum):
    WAITING_CORE = "waiting_core"
    LOADING_MODULES = "loading_modules"
    READY = "ready"
    FAILED = "failed"


# Status vocabulary shared with both UIs (translated client-side as ``boot.status.*``).
ONLINE = "online"            # dependency is up and usable
PENDING = "pending"          # actively connecting / bootstrapping right now
WAITING = "waiting"          # blocked by an upstream dependency, not started yet
ACTION = "action"            # operator action needed (Vault init / unseal)
FAILED = "failed"            # gave up — see logs
UNAVAILABLE = "unavailable"  # not provided by this core version (cluster rows)

CORE_SERVICES = ("vault", "database", "iam", "modules")
CLUSTER_SERVICES = ("coordinator", "leader", "peers", "storage")


class BootService:
    def __init__(self):
        self.is_booting = True
        self.phase: BootPhase = BootPhase.WAITING_CORE
        self.last_error: str | None = None
        self.started_at = datetime.now(timezone.utc)
        self._started_mono = time.monotonic()
        # IAM has no readiness flag of its own — it only emits events. Track them
        # here so the snapshot can tell "bootstrapping" from "ready" and "failed".
        self.iam_ready = False
        self.iam_error: str | None = None
        self._since: Dict[str, str] = {}
        self._last_status: Dict[str, str] = {}
        bus.subscribe("iam:ready")(self.on_core_ready)
        bus.subscribe("iam:failed")(self.on_iam_failed)

    def _set_state(
        self,
        phase: BootPhase,
        *,
        is_booting: bool | None = None,
        error: str | None = None,
    ):
        if is_booting is not None:
            self.is_booting = is_booting
        self.phase = phase
        self.last_error = error
        bus.emit(
            "system:boot_phase",
            {"phase": phase.value, "error": error, "is_booting": self.is_booting},
        )

    async def on_iam_failed(self, payload):
        self.iam_error = str((payload or {}).get("error") or "unknown")

    async def on_core_ready(self, payload):
        self.iam_ready = True
        self.iam_error = None
        log.info("STARTUP: All core systems (Vault, DB, IAM) are online.")
        log.info("LOAD: Loading system modules and plugins...")
        self._set_state(BootPhase.LOADING_MODULES, is_booting=True)
        try:
            # Local import breaks the circular dependency; it only runs when this
            # coroutine actually executes.
            from core.components.plugins.logic.manager import module_manager

            # TODO(agent): load_all() performs blocking filesystem scans/imports on
            # the event loop. It cannot be moved to asyncio.to_thread as-is because
            # it schedules loop tasks (bus.create_tracked_task) internally; make it
            # natively async (offload only the blocking scan) in a follow-up.
            module_manager.load_all()

            self._set_state(BootPhase.READY, is_booting=False)
            log.info("SUCCESS: Boot sequence completed. System released.")

            # Signal all interested plugins that boot has finished.
            bus.emit("system:boot_complete", {"status": "success"})
        except Exception as e:
            self._set_state(BootPhase.FAILED, is_booting=False, error=str(e))
            log.error(
                f"ERROR: Boot sequence failed while loading modules: {e}",
                exc_info=True,
            )
            bus.emit(
                "system:maintenance_mode",
                {"service": "boot", "active": True, "reason": "boot_failed"},
            )

    # ------------------------------------------------------------------
    # Boot dependency snapshot (GET /api/boot, NiceGUI boot page)
    # ------------------------------------------------------------------

    @staticmethod
    def _row(
        service_id: str,
        status: str,
        detail: Optional[str],
        params: Optional[Dict[str, Any]] = None,
        progress: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        return {
            "id": service_id,
            "status": status,
            "detail": detail,
            "detail_params": params or {},
            "progress": progress,
        }

    def _vault_row(self, vault) -> Dict[str, Any]:
        state = getattr(vault, "ui_state", "loading")
        mapping = {
            "loading": (PENDING, "connecting"),
            "needs_init": (ACTION, "needs_init"),
            "needs_unseal": (ACTION, "sealed"),
            "ready": (ONLINE, "unsealed"),
            "error": (FAILED, "vault_error"),
        }
        status, detail = mapping.get(state, (PENDING, "connecting"))
        return self._row("vault", status, detail)

    def _db_row(self, db, vault_online: bool) -> Dict[str, Any]:
        if getattr(db, "is_connected", False):
            return self._row("database", ONLINE, "connected")
        max_retries = int(getattr(db, "max_retries", 0) or 0)
        reason = getattr(db, "failure_reason", None)
        if reason == "config_error":
            return self._row("database", FAILED, "db_config_error")
        if reason == "max_retries":
            return self._row("database", FAILED, "db_gave_up", {"max": max_retries})
        if not vault_online:
            return self._row("database", WAITING, "waiting_for", {"service": "vault"})
        if getattr(db, "was_connected", False):
            return self._row("database", PENDING, "reconnecting")
        attempts = int(getattr(db, "connect_attempts", 0) or 0)
        if attempts > 0:
            return self._row("database", PENDING, "db_retry", {"attempt": attempts, "max": max_retries})
        return self._row("database", PENDING, "connecting")

    def _iam_row(self, db_online: bool) -> Dict[str, Any]:
        if self.iam_ready:
            return self._row("iam", ONLINE, "iam_ready")
        if self.iam_error:
            return self._row("iam", FAILED, "iam_failed")
        if db_online:
            return self._row("iam", PENDING, "iam_bootstrapping")
        return self._row("iam", WAITING, "waiting_for", {"service": "database"})

    def _modules_row(self) -> Dict[str, Any]:
        if self.phase == BootPhase.READY:
            total = active = failed = 0
            try:
                from core.components.plugins.logic.manager import module_manager

                for entry in module_manager.registry.values():
                    total += 1
                    status = str(entry.get("status") or "")
                    if status == "active":
                        active += 1
                    elif status == "failed":
                        failed += 1
            except Exception:  # pragma: no cover - defensive, snapshot must never raise
                pass
            return self._row(
                "modules", ONLINE, "modules_summary",
                {"active": active, "failed": failed, "total": total},
            )
        if self.phase == BootPhase.FAILED:
            return self._row("modules", FAILED, "modules_failed")
        if self.phase == BootPhase.LOADING_MODULES:
            return self._row("modules", PENDING, "modules_loading")
        return self._row("modules", WAITING, "waiting_for", {"service": "iam"})

    def _stamp_since(self, services: List[Dict[str, Any]]) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        for svc in services:
            sid, status = svc["id"], svc["status"]
            if self._last_status.get(sid) != status:
                self._last_status[sid] = status
                self._since[sid] = now_iso
            svc["since"] = self._since.get(sid)

    def snapshot(self) -> Dict[str, Any]:
        """Public, cheap view of the boot dependencies. Never raises, never
        touches Vault or the DB, never leaks error text (anonymous callers)."""
        # Lazy imports: core.services imports this module at import time.
        from config import settings
        from core.services import db_instance, vault_instance

        mode = "cluster" if str(getattr(settings, "LYNDRIX_CLUSTER_MODE", "single")).lower() == "cluster" else "single"
        vault_state = getattr(vault_instance, "ui_state", "loading")
        vault_online = vault_state == "ready"
        db_online = bool(getattr(db_instance, "is_connected", False))

        services = [
            self._vault_row(vault_instance),
            self._db_row(db_instance, vault_online),
            self._iam_row(db_online),
            self._modules_row(),
        ]
        if mode == "cluster":
            # The cluster coordinator is not part of this core version yet; say so
            # explicitly instead of hiding the rows (see features/high-availability*.md).
            services.extend(
                self._row(sid, UNAVAILABLE, "cluster_not_available") for sid in CLUSTER_SERVICES
            )
        self._stamp_since(services)

        online = sum(1 for s in services if s["status"] == ONLINE)
        return {
            "phase": self.phase.value,
            "is_booting": self.is_booting,
            "ready": self.phase == BootPhase.READY and not self.is_booting,
            "failed": self.phase == BootPhase.FAILED,
            "mode": mode,
            "vault_ui_state": vault_state,
            "started_at": self.started_at.isoformat(),
            "elapsed_s": round(time.monotonic() - self._started_mono, 1),
            "summary": {"online": online, "total": len(services)},
            "services": services,
        }


boot_service = BootService()
