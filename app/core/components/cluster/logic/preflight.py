"""Preflight — validates the HA *contracts* instead of prescribing one setup.

Each check returns ``ok``, ``warn``, ``fail`` or ``skip`` with a plain-text
message and a hint. ``fail`` is reserved for states in which routing traffic to
this instance would be wrong (different ``STORAGE_SECRET``, no instance for a
required role, coordinator down) — those keep ``/api/ready`` at 503.
Everything that merely degrades the experience is a ``warn``.

Runs once on ``system:boot_complete`` and on demand via
``GET /api/cluster/preflight`` (also the Cluster page's "Re-run" button).
"""
from __future__ import annotations

import asyncio
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

from config import settings
from core.logger import get_logger

if TYPE_CHECKING:  # pragma: no cover
    from .cluster_service import ClusterService

log = get_logger("Core:Cluster:Preflight")

_VAULT_PROBE_TIMEOUT_S = 3
_CLOCK_DRIFT_WARN_S = 5.0
# sys/health answers with these codes for initialized/sealed/standby/… nodes —
# any of them means "reachable".
_VAULT_HEALTH_CODES = {200, 429, 472, 473, 501, 503}


@dataclass
class Check:
    id: str
    status: str  # ok | warn | fail | skip
    message: str
    hint: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


def _probe_vault(url: str) -> tuple[bool, str]:
    target = f"{url.rstrip('/')}/v1/sys/health"
    try:
        with urllib.request.urlopen(target, timeout=_VAULT_PROBE_TIMEOUT_S) as resp:  # noqa: S310 - operator-provided URLs
            return True, f"HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        return exc.code in _VAULT_HEALTH_CODES, f"HTTP {exc.code}"
    except Exception as exc:
        return False, str(exc)[:80]


def _pinned_roles() -> dict[str, list[str]]:
    """role → plugin ids that are pinned to it (active, remote or waiting)."""
    out: dict[str, list[str]] = {}
    try:
        from core.components.plugins.logic.manager import module_manager

        for mid, entry in module_manager.registry.items():
            manifest = entry.get("manifest")
            if not manifest or manifest.type != "PLUGIN":
                continue
            if entry.get("status") in ("disabled", "degraded", "failed"):
                continue
            placement = getattr(manifest, "placement", "any") or "any"
            if placement.startswith("pinned:"):
                out.setdefault(placement.split(":", 1)[1], []).append(mid)
    except Exception:
        pass
    return out


async def run_checks(service: "ClusterService") -> list[Check]:
    checks: list[Check] = []
    cluster = service.is_cluster

    def add(check_id: str, status: str, message: str, hint: Optional[str] = None) -> None:
        checks.append(Check(check_id, status, message, hint))

    try:
        instances = await service.refresh_instances()
    except Exception as exc:
        instances = []
        add("coordinator", "fail" if cluster else "warn", f"coordinator unreachable: {str(exc)[:120]}",
            "Check DB connectivity; the cluster_* tables are created automatically on first start.")
    alive = [i for i in instances if i.alive]

    # 1. coordinator
    if not any(c.id == "coordinator" for c in checks):
        if not cluster:
            add("coordinator", "skip", "single-instance mode — coordinator driver 'local'")
        elif service.started:
            add("coordinator", "ok", f"db coordinator connected, {len(alive)} live instance(s)")
        else:
            add("coordinator", "fail", f"coordinator not started: {service.start_error or 'unknown error'}",
                "Check DB connectivity; the cluster_* tables are created automatically on first start.")

    # 2. redundancy
    if cluster:
        if len(alive) >= 2:
            add("redundancy", "ok", f"{len(alive)} live instances")
        else:
            add("redundancy", "warn", "only this instance is alive — no redundancy yet",
                "Start a second core instance with the same DB, Vault and STORAGE_SECRET.")
    else:
        add("redundancy", "skip", "single-instance mode")

    # 3. STORAGE_SECRET identical
    fingerprints = {i.meta.get("secret_fp") for i in alive if i.meta.get("secret_fp")}
    if cluster and len(fingerprints) > 1:
        add("storage_secret", "fail", "STORAGE_SECRET differs between live instances",
            "Sessions and SSE tickets minted on one instance are rejected by the others — "
            "set the same STORAGE_SECRET on every instance.")
    elif cluster:
        add("storage_secret", "ok", "STORAGE_SECRET fingerprint identical on all live instances")
    else:
        add("storage_secret", "skip", "single-instance mode")

    # 4. versions
    versions = {f"{i.core_version}/{i.api_version}" for i in alive if i.core_version}
    if cluster and len(versions) > 1:
        add("versions", "warn", f"mixed core versions: {', '.join(sorted(versions))}",
            "Expected during a rolling update; otherwise redeploy the stragglers.")
    elif cluster:
        add("versions", "ok", f"all instances run core {next(iter(versions), '?')}")
    else:
        add("versions", "skip", "single-instance mode")

    # 5. plugin artifacts
    plugin_fps = {i.meta.get("plugins_fp") for i in alive if i.meta.get("plugins_fp")}
    if cluster and len(plugin_fps) > 1:
        add("plugin_artifacts", "warn", "the loaded plugin set differs between instances",
            "Plugins reconcile from plugin_states on plugin:desired_changed and every 5 min; "
            "compare GET /api/plugins per instance if it does not converge.")
    elif cluster:
        add("plugin_artifacts", "ok", "identical plugin set on all live instances")
    else:
        add("plugin_artifacts", "skip", "single-instance mode")

    # 6. role coverage
    alive_roles = {r for i in alive for r in i.roles} | set(service.roles)
    pinned = _pinned_roles()
    missing = {role: ids for role, ids in pinned.items() if role not in alive_roles}
    if missing:
        add("roles", "fail", "no live instance has role(s) " + ", ".join(
            f"'{role}' (needed by {', '.join(ids)})" for role, ids in sorted(missing.items())),
            "Start an instance with LYNDRIX_INSTANCE_ROLES including that role, or change the plugin placement.")
    elif cluster and "leader" not in alive_roles:
        add("roles", "fail", "no leader-capable instance is alive",
            "At least one instance needs the 'leader' role for seeds, reconcilers and singleton plugins.")
    else:
        add("roles", "ok", "every pinned role and the leader role are covered" if cluster else "roles: " + ",".join(service.roles))

    # 7. UI engine
    engines = {e.strip().lower() for e in (settings.LYNDRIX_UI_ENGINE or "").split(",")}
    if cluster and ("nicegui" in engines or "both" in engines):
        add("ui_engine", "warn", "NiceGUI is enabled in cluster mode",
            "The legacy UI is websocket/process-bound and needs sticky sessions — run LYNDRIX_UI_ENGINE=react behind a load balancer.")
    else:
        add("ui_engine", "ok", f"UI engine: {settings.LYNDRIX_UI_ENGINE}")

    # 8. trusted proxies
    proxies = (settings.LYNDRIX_TRUSTED_PROXIES or "").strip()
    default_proxies = proxies in ("", "127.0.0.1")
    if service.saw_forwarded_for and default_proxies:
        add("trusted_proxies", "warn", "X-Forwarded-For arrives but LYNDRIX_TRUSTED_PROXIES is the default",
            "The login rate limiter and audit entries see the proxy's IP — set LYNDRIX_TRUSTED_PROXIES to the load balancer address(es) or '*'.")
    elif cluster and default_proxies:
        add("trusted_proxies", "warn", "LYNDRIX_TRUSTED_PROXIES is the default (127.0.0.1)",
            "Set it to your load balancer address(es) so forwarded client IPs are honoured.")
    else:
        add("trusted_proxies", "ok", f"trusted proxies: {proxies or 'none'}")

    # 9. database topology / isolation
    try:
        info = await service.coordinator.server_info()
        if info.get("wsrep_on") is True and not settings.DB_ALLOW_DIRECT_NODE:
            add("database", "warn", "DB_HOST is a Galera node (wsrep_on=ON) addressed directly",
                "Route all writes through one MaxScale/ProxySQL endpoint (single writer); set DB_ALLOW_DIRECT_NODE=1 to silence.")
        elif info.get("error"):
            add("database", "warn", f"could not inspect the database server: {info['error']}")
        else:
            extra = " (snapshot isolation on → coordinator uses READ COMMITTED)" if info.get("snapshot_isolation") else ""
            add("database", "ok", f"{info.get('dialect', 'db')} {info.get('version', '')}{extra}".strip())
    except Exception as exc:
        add("database", "warn", f"database inspection failed: {str(exc)[:100]}")

    # 10. clock drift vs. the DB clock (leases are compared on the DB clock)
    try:
        db_now = await service.coordinator.db_now()
        local = datetime.now(timezone.utc).replace(tzinfo=None)
        drift = abs((local - db_now).total_seconds())
        if drift > _CLOCK_DRIFT_WARN_S:
            add("clock", "warn", f"this instance's clock is {drift:.1f}s off the database clock",
                "Leases use the DB clock, so this is harmless for coordination, but log timestamps and TTL displays will disagree — fix NTP/chrony.")
        else:
            add("clock", "ok", f"clock drift to the database: {drift:.1f}s")
    except Exception as exc:
        add("clock", "skip", f"clock drift check unavailable: {str(exc)[:80]}")

    # 11. Vault nodes
    if settings.VAULT_NODES:
        results = await asyncio.gather(*(asyncio.to_thread(_probe_vault, node) for node in settings.vault_nodes))
        unreachable = [node for node, (ok, _) in zip(settings.vault_nodes, results) if not ok]
        if unreachable:
            add("vault_nodes", "warn", f"{len(unreachable)} of {len(results)} Vault node(s) unreachable: {', '.join(unreachable)}",
                "Each node in VAULT_NODES must answer /v1/sys/health from every core instance.")
        else:
            add("vault_nodes", "ok", f"all {len(results)} Vault node(s) reachable")
    else:
        add("vault_nodes", "skip", "VAULT_NODES not set — single Vault endpoint (VAULT_URL)")

    # 12. drain
    if cluster and service.drain_seconds <= 0:
        add("drain", "warn", "LYNDRIX_DRAIN_SECONDS=0 — a stopping instance drops in-flight requests",
            "Set LYNDRIX_DRAIN_SECONDS to at least two load-balancer health-check intervals (default 10).")
    else:
        add("drain", "ok", f"drain on SIGTERM: {service.drain_seconds}s" if service.drain_seconds else "no drain (single instance)")

    # 13. shared storage (not shipped yet — be explicit)
    if cluster:
        add("storage", "warn", "no shared storage driver in this core version — plugin files stay instance-local",
            "Attachments/uploads written on one instance are not visible on the others; use a shared mount for /data/storage until the storage driver ships.")
    else:
        add("storage", "skip", "single-instance mode — local storage")

    return checks
