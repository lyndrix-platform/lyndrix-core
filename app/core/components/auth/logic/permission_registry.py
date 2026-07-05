"""
Permission registry.

Central source of truth for all permission identifiers in the system.

Permissions are grouped into categories:
  route   — access to a specific UI route  (auto-scanned from manifests)
  plugin  — a specific plugin is accessible (auto-scanned from manifests)
  feature — built-in feature gates (static list, extendable via register())
  api     — API-level read/write gates

Usage in views / API guards:
  from core.components.auth.logic.permission_registry import permission_registry
  from core.components.auth.logic.group_service import group_service

  # Check if the session user may access a route
  user_roles = app.storage.user.get('roles', [])
  user_extra  = app.storage.user.get('extra_permissions', [])
  if not group_service.user_has_permission(user_roles, 'route:/iac-orchestrator', user_extra):
      ui.navigate.to('/login')
      return
"""
from dataclasses import dataclass
from typing import Dict, List, Optional

from core.logger import get_logger

log = get_logger("Core:PermissionRegistry")


@dataclass
class PermissionDef:
    id: str           # e.g. "route:/iac-orchestrator" or "plugin:<id>:api:read"
    label: str        # "IAC Orchestrator"
    category: str     # "route" | "plugin" | "feature" | "api"
    description: str = ""
    icon: str = "lock"
    # Owning plugin/module id for namespaced permissions — lets the UI group
    # the catalog per plugin. None for core/builtin ids.
    plugin_id: Optional[str] = None


# ── Built-in feature / api permissions ────────────────────────────────────────
_BUILTIN: List[PermissionDef] = [
    PermissionDef("feature:dashboard.view",   "Dashboard ansehen",             "feature", icon="dashboard"),
    PermissionDef("feature:settings.view",    "Einstellungen ansehen",         "feature", icon="settings"),
    PermissionDef("feature:settings.edit",    "Einstellungen bearbeiten",      "feature", icon="settings"),
    PermissionDef("feature:users.view",       "Benutzer ansehen",              "feature", icon="people"),
    PermissionDef("feature:users.edit",       "Benutzer verwalten",            "feature", icon="manage_accounts"),
    PermissionDef("feature:groups.view",      "Gruppen ansehen",               "feature", icon="group"),
    PermissionDef("feature:groups.edit",      "Gruppen verwalten",             "feature", icon="group"),
    PermissionDef("feature:auth.configure",   "Auth-Provider konfigurieren",   "feature", icon="security"),
    PermissionDef("feature:plugins.manage",   "Plugins verwalten",             "feature", icon="extension"),
    PermissionDef("feature:dashboard.admin_sections", "Dashboard: Core/Services-Bereiche", "feature", icon="dashboard_customize",
                  description="Sichtbarkeit der Core- und Services-Sektionen im Dashboard (Viewer sehen nur Apps & Tools)"),
    PermissionDef("api:read",                 "API lesen",                     "api",     icon="api"),
    PermissionDef("api:write",                "API schreiben",                 "api",     icon="api"),
    PermissionDef("admin:read",               "Administration ansehen",        "api",     icon="admin_panel_settings",
                  description="Lesender Zugriff auf administrative Endpunkte (Benutzer, Gruppen, Logs, Plugin- und Systemverwaltung)"),
    PermissionDef("admin:write",              "Administration verwalten",      "api",     icon="admin_panel_settings",
                  description="Schreibender Zugriff auf administrative Endpunkte (Benutzer-, Gruppen- und Systemverwaltung)"),
]

_CATEGORY_ORDER = ["route", "plugin", "feature", "api"]
_CATEGORY_LABELS = {
    "route":   "Routen",
    "plugin":  "Plugins",
    "feature": "Features",
    "api":     "API",
}
_CATEGORY_COLORS = {
    "route":   "text-sky-400",
    "plugin":  "text-violet-400",
    "feature": "text-emerald-400",
    "api":     "text-amber-400",
}


class PermissionRegistry:
    def __init__(self) -> None:
        self._perms: Dict[str, PermissionDef] = {}
        for p in _BUILTIN:
            self._perms[p.id] = p

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register(self, pdef: PermissionDef) -> None:
        self._perms[pdef.id] = pdef

    # ------------------------------------------------------------------
    # Lookup
    # ------------------------------------------------------------------

    def get(self, perm_id: str) -> Optional[PermissionDef]:
        return self._perms.get(perm_id)

    def get_all(self) -> List[PermissionDef]:
        return sorted(
            self._perms.values(),
            key=lambda p: (_CATEGORY_ORDER.index(p.category) if p.category in _CATEGORY_ORDER else 99, p.id),
        )

    def get_by_category(self, category: str) -> List[PermissionDef]:
        return [p for p in self.get_all() if p.category == category]

    def categories(self) -> List[str]:
        present = {p.category for p in self._perms.values()}
        return [c for c in _CATEGORY_ORDER if c in present]

    def category_label(self, category: str) -> str:
        return _CATEGORY_LABELS.get(category, category.capitalize())

    def category_color(self, category: str) -> str:
        return _CATEGORY_COLORS.get(category, "text-zinc-400")

    # ------------------------------------------------------------------
    # Auto-scan
    # ------------------------------------------------------------------

    def scan_from_manifests(self) -> None:
        """
        Register route + plugin permissions for every active module manifest.
        Safe to call multiple times — existing entries are overwritten.
        Called lazily when the Permissions tab is first rendered.

        Identity & Permissions 2.0 additions per manifest:
          - ``plugin:<id>:route:<path>`` for every ``react_routes`` entry
            (previously only ``ui_route`` was scanned — granting a React-only
            route like ``route:/bingo`` 422'd as unknown)
          - ``plugin:<id>:api:read`` / ``plugin:<id>:api:write`` unconditionally
            (per-plugin API granularity, no author action required; global
            api:read/write remain a compat fallback in access_service)
          - ``plugin:<id>:<suffix>`` for each ``custom_permissions`` entry
          - manifest ``roles`` are forwarded to the role_registry
        """
        try:
            from core.components.plugins.logic.manager import module_manager
        except Exception as e:
            log.warning(f"PERMS: Could not import module_manager: {e}")
            return

        for manifest in module_manager.get_manifests():
            icon = getattr(manifest, "icon", None) or "extension"
            route = getattr(manifest, "ui_route", None)
            if route:
                self.register(PermissionDef(
                    id=f"route:{route}",
                    label=manifest.name,
                    category="route",
                    description=f"Zugriff auf Route {route}",
                    icon=getattr(manifest, "icon", None) or "route",
                    plugin_id=manifest.id,
                ))
            self.register(PermissionDef(
                id=f"plugin:{manifest.id}",
                label=f"{manifest.name} (Plugin)",
                category="plugin",
                description=f"Plugin {manifest.id} ist nutzbar",
                icon=icon,
                plugin_id=manifest.id,
            ))

            # React routes → per-plugin route permissions.
            for rr in getattr(manifest, "react_routes", None) or []:
                path = (rr or {}).get("path")
                if not path:
                    continue
                self.register(PermissionDef(
                    id=f"plugin:{manifest.id}:route:{path}",
                    label=f"{manifest.name}: {rr.get('label') or path}",
                    category="route",
                    description=f"Zugriff auf React-Route {path}",
                    icon=rr.get("icon") or icon,
                    plugin_id=manifest.id,
                ))

            # Per-plugin API read/write — free granularity for every module.
            for verb, de_label in (("read", "lesen"), ("write", "schreiben")):
                self.register(PermissionDef(
                    id=f"plugin:{manifest.id}:api:{verb}",
                    label=f"{manifest.name}: API {de_label}",
                    category="api",
                    description=f"API-{verb}-Zugriff auf Plugin {manifest.id}",
                    icon="api",
                    plugin_id=manifest.id,
                ))

            # Plugin-declared fine-grained permissions.
            for cp in getattr(manifest, "custom_permissions", None) or []:
                self.register(PermissionDef(
                    id=f"plugin:{manifest.id}:{cp.id}",
                    label=f"{manifest.name}: {cp.label}",
                    category="api",
                    description=cp.description,
                    icon=cp.icon or icon,
                    plugin_id=manifest.id,
                ))

        # Manifest roles live in their own registry (bundles, not checkable ids).
        try:
            from .role_registry import role_registry

            role_registry.scan_from_manifests()
        except Exception as e:  # pragma: no cover - defensive
            log.warning(f"PERMS: role registry scan failed: {e}")


# Singleton
permission_registry = PermissionRegistry()
