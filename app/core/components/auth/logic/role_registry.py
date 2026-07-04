"""Role registry — manifest-declared permission bundles (Identity 2.0).

Roles are NOT database entities and NOT checkable permission ids. A
``ManifestRole`` is declarative metadata: "this plugin's *admin* role means
these permissions, and it should attach to these groups by default". The
registry mirrors ``PermissionRegistry`` (in-memory, rebuilt from manifests),
while the *effect* of a role — its permissions landing in a group's list — is
applied exactly once per (role, group) pair, recorded in the
``RoleGrantLedger`` table so an admin's later revocation is never silently
re-applied on reboot or plugin re-activation.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from core.logger import get_logger

log = get_logger("Core:RoleRegistry")


@dataclass
class RoleDef:
    id: str                      # "role:<plugin_id>:<suffix>"
    label: str
    plugin_id: str
    permissions: List[str] = field(default_factory=list)   # fully-qualified ids
    auto_map_groups: List[str] = field(default_factory=list)
    description: str = ""


def _qualify(plugin_id: str, perm: str) -> str:
    """Namespace a local suffix; pass fully-qualified ids through verbatim."""
    if perm.startswith(("api:", "feature:", "route:", "plugin:")):
        return perm
    return f"plugin:{plugin_id}:{perm}"


class RoleRegistry:
    def __init__(self) -> None:
        self._roles: Dict[str, RoleDef] = {}

    def register(self, rdef: RoleDef) -> None:
        self._roles[rdef.id] = rdef

    def get(self, role_id: str) -> Optional[RoleDef]:
        return self._roles.get(role_id)

    def get_all(self) -> List[RoleDef]:
        return sorted(self._roles.values(), key=lambda r: r.id)

    def scan_from_manifests(self) -> None:
        """Rebuild role defs from every loaded manifest (idempotent)."""
        try:
            from core.components.plugins.logic.manager import module_manager
        except Exception as e:  # pragma: no cover - defensive
            log.warning(f"ROLES: Could not import module_manager: {e}")
            return

        for manifest in module_manager.get_manifests():
            for role in getattr(manifest, "roles", None) or []:
                self.register(RoleDef(
                    id=f"role:{manifest.id}:{role.id}",
                    label=role.label,
                    plugin_id=manifest.id,
                    permissions=[_qualify(manifest.id, p) for p in role.permissions],
                    auto_map_groups=list(role.auto_map_groups),
                    description=role.description,
                ))

    # ------------------------------------------------------------------
    # Auto-mapping (ledger-guarded, applied at IAM bootstrap + plugin enable)
    # ------------------------------------------------------------------

    def apply_auto_mappings(self) -> int:
        """Attach each role's permissions to its auto_map groups — once.

        Returns the number of (role, group) pairs newly applied. Unknown
        groups are skipped with a warning (they may be seeded later; the next
        pass picks them up because no ledger row was written). Never raises.
        """
        applied = 0
        try:
            from core.components.database.logic.db_service import db_instance
            from .models import Group, RoleGrantLedger
            from . import access_service
        except Exception as e:  # pragma: no cover - defensive
            log.warning(f"ROLES: auto-mapping unavailable: {e}")
            return 0

        try:
            with db_instance.SessionLocal() as s:
                done = {
                    (row.role_id, row.group_name)
                    for row in s.query(RoleGrantLedger).all()
                }
                for role in self.get_all():
                    for group_name in role.auto_map_groups:
                        if (role.id, group_name) in done:
                            continue
                        grp = s.query(Group).filter(Group.name == group_name).first()
                        if grp is None:
                            log.warning(
                                f"ROLES: auto-map target group '{group_name}' for "
                                f"{role.id} does not exist yet — skipping."
                            )
                            continue
                        perms = list(grp.permissions or [])
                        added = [p for p in role.permissions if p not in perms]
                        if added:
                            grp.permissions = sorted(perms + added)
                        s.add(RoleGrantLedger(role_id=role.id, group_name=group_name))
                        applied += 1
                        log.info(
                            f"ROLES: applied {role.id} -> {group_name} "
                            f"(+{len(added)} permission(s))"
                        )
                if applied:
                    s.commit()
        except Exception as e:  # pragma: no cover - defensive
            log.warning(f"ROLES: auto-mapping failed: {e}")
            return applied

        if applied:
            access_service.invalidate_cache()
        return applied


    def apply_baseline_grants(self) -> int:
        """Zero-config per-plugin visibility for the default INT_* groups.

        For every PLUGIN manifest (once per plugin+group, ledger key
        ``baseline:<plugin_id>``): ``plugin:<id>`` + ``plugin:<id>:api:read``
        to all three default groups, ``plugin:<id>:api:write`` to INT_USER and
        INT_ADMIN. Plugins needing stricter defaults declare a ManifestRole
        with a narrower ``auto_map_groups`` on top. Never raises.
        """
        grants: dict[str, tuple[str, ...]] = {
            "INT_VIEWER": ("plugin:{id}", "plugin:{id}:api:read"),
            "INT_USER": ("plugin:{id}", "plugin:{id}:api:read", "plugin:{id}:api:write"),
            "INT_ADMIN": ("plugin:{id}", "plugin:{id}:api:read", "plugin:{id}:api:write"),
        }
        applied = 0
        try:
            from core.components.database.logic.db_service import db_instance
            from core.components.plugins.logic.manager import module_manager
            from .models import Group, RoleGrantLedger
            from . import access_service
        except Exception as e:  # pragma: no cover - defensive
            log.warning(f"ROLES: baseline grants unavailable: {e}")
            return 0

        try:
            plugin_ids = [
                m.id for m in module_manager.get_manifests()
                if getattr(m, "type", "PLUGIN") == "PLUGIN"
            ]
            with db_instance.SessionLocal() as s:
                done = {
                    (row.role_id, row.group_name)
                    for row in s.query(RoleGrantLedger).all()
                }
                for pid in plugin_ids:
                    ledger_id = f"baseline:{pid}"
                    for group_name, templates in grants.items():
                        if (ledger_id, group_name) in done:
                            continue
                        grp = s.query(Group).filter(Group.name == group_name).first()
                        if grp is None:
                            continue  # default group not seeded yet — next pass
                        perms = list(grp.permissions or [])
                        added = [
                            t.format(id=pid) for t in templates
                            if t.format(id=pid) not in perms
                        ]
                        if added:
                            grp.permissions = sorted(perms + added)
                        s.add(RoleGrantLedger(role_id=ledger_id, group_name=group_name))
                        applied += 1
                if applied:
                    s.commit()
        except Exception as e:  # pragma: no cover - defensive
            log.warning(f"ROLES: baseline grants failed: {e}")
            return applied

        if applied:
            log.info(f"ROLES: applied baseline plugin grants ({applied} pair(s)).")
            access_service.invalidate_cache()
        return applied

    def sync(self) -> None:
        """Full pass: rebuild defs from manifests, then apply baseline + role
        auto-mappings. Called on system:boot_complete and plugin activation."""
        self.scan_from_manifests()
        self.apply_baseline_grants()
        self.apply_auto_mappings()


# Singleton
role_registry = RoleRegistry()
