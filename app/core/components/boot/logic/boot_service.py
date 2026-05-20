import asyncio
from enum import StrEnum

from core.bus import bus
from core.logger import get_logger

log = get_logger("Core:BootService")


class BootPhase(StrEnum):
    WAITING_CORE = "waiting_core"
    LOADING_MODULES = "loading_modules"
    READY = "ready"
    FAILED = "failed"


class BootService:
    def __init__(self):
        self.is_booting = True
        self.phase: BootPhase = BootPhase.WAITING_CORE
        self.last_error: str | None = None
        bus.subscribe("iam:ready")(self.on_core_ready)

    def _set_phase(self, phase: BootPhase, *, error: str | None = None):
        self.phase = phase
        self.last_error = error
        bus.emit(
            "system:boot_phase",
            {"phase": phase.value, "error": error, "is_booting": self.is_booting},
        )

    async def on_core_ready(self, payload):
        log.info("STARTUP: All core systems (Vault, DB, IAM) are online.")
        log.info("LOAD: Loading system modules and plugins...")
        self._set_phase(BootPhase.LOADING_MODULES)
        try:
            # ---------------------------------------------------------
            # LOKALER IMPORT: Löst den Zirkelbezug auf!
            # Wird erst ausgeführt, wenn die Funktion wirklich läuft.
            # ---------------------------------------------------------
            from core.components.plugins.logic.manager import module_manager

            module_manager.load_all()

            await asyncio.sleep(0.5)
            self.is_booting = False
            self._set_phase(BootPhase.READY)
            log.info("SUCCESS: Boot sequence completed. System released.")

            # NEU: Das Signal an alle interessierten Plugins senden
            bus.emit("system:boot_complete", {"status": "success"})
        except Exception as e:
            self.is_booting = False
            self._set_phase(BootPhase.FAILED, error=str(e))
            log.error(
                f"ERROR: Boot sequence failed while loading modules: {e}",
                exc_info=True,
            )
            bus.emit(
                "system:maintenance_mode",
                {"service": "boot", "active": True, "reason": "boot_failed"},
            )


boot_service = BootService()
