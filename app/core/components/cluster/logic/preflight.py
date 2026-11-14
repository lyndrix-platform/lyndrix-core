"""Preflight — validates the HA *contracts* instead of prescribing one setup.

Each check returns ``ok``, ``warn``, ``fail`` or ``skip`` with a plain-text
message and a hint. ``fail`` is reserved for states in which routing traffic to
this instance would be wrong (a ``STORAGE_SECRET`` that differs from the
cluster's, no instance for a required role, coordinator not started).
Everything that merely degrades the experience is a ``warn``.

Runs once on ``system:boot_complete``, then in its own task every five minutes
and whenever the membership changes, and on demand via
``GET /api/cluster/preflight`` (also the Cluster page's "Re-run" button).

Two rules keep the preflight from doing harm itself:

* every check that does I/O is bounded (:data:`CHECK_TIMEOUT_S`). A check that
  times out or hits a transient error reports ``warn`` and asks for a re-run
  soon (``Check.recheck``) — it never turns into a readiness failure;
* ``/api/ready`` reads coordinator health live (see
  ``ClusterService.readiness``); the cached result of the ``coordinator`` check
  is display only.
"""
from __future__ import annotations

import asyncio
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Awaitable, Optional

from config import settings
from core.logger import get_logger

from .storage import StorageBusy

if TYPE_CHECKING:  # pragma: no cover
    from .cluster_service import ClusterService

log = get_logger("Core:Cluster:Preflight")

_VAULT_PROBE_TIMEOUT_S = 3
_CLOCK_DRIFT_WARN_S = 5.0
# sys/health answers with these codes for initialized/sealed/standby/… nodes —
# any of them means "reachable".
_VAULT_HEALTH_CODES = {200, 429, 472, 473, 501, 503}
# Upper bound for one I/O check (database round trip, Vault probes, storage
# listing). The drivers' own timeouts are far longer (socket timeout 90 s; a
# hung NFS mount: none) — the preflight must not wait for them.
CHECK_TIMEOUT_S = 8.0


@dataclass
class Check:
    id: str
    status: str  # ok | warn | fail | skip
    message: str
    hint: Optional[str] = None
    # Internal: this result is provisional (timeout, transient error, peer too
    # young to judge) — run the preflight again in a couple of heartbeats.
    recheck: bool = field(default=False, repr=False)

    def to_dict(self) -> dict:
        return {"id": self.id, "status": self.status, "message": self.message, "hint": self.hint}


async def _bounded(awaitable: Awaitable[Any]) -> Any:
    return await asyncio.wait_for(awaitable, timeout=CHECK_TIMEOUT_S)


def _why(exc: BaseException) -> str:
    if isinstance(exc, asyncio.TimeoutError):
        return f"no answer within {CHECK_TIMEOUT_S:.0f}s"
    return str(exc)[:120] or exc.__class__.__name__


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


def _secret_reference(alive: list, own_fp: Optional[str]) -> tuple[Optional[str], list[str], int]:
    """The STORAGE_SECRET fingerprint the cluster agrees on, and who deviates.

    Reference = the fingerprint most live instances carry; a tie goes to the
    group holding the instance that has been running longest (then the lowest
    id, so every instance computes the same answer from the same rows). The
    leader is deliberately *not* the reference: a joiner with a wrong secret can
    win the leader lease. Returns ``(reference, [instance ids that differ],
    number of instances carrying the reference)``.
    """
    fingerprints: dict[str, Optional[str]] = {}
    for inst in alive:
        # This instance's own value is authoritative even when its row is stale.
        fp = own_fp if (inst.is_self and own_fp) else inst.meta.get("secret_fp")
        if fp:
            fingerprints[inst.instance_id] = fp
    if not fingerprints:
        return None, [], 0

    def _age_rank(inst) -> tuple:
        started = inst.started_at
        # Unknown start time sorts last (youngest); the id breaks remaining ties.
        return (started is None, started or 0, inst.instance_id)

    groups: dict[str, list] = {}
    for inst in alive:
        fp = fingerprints.get(inst.instance_id)
        if fp:
            groups.setdefault(fp, []).append(inst)
    reference = min(groups, key=lambda fp: (-len(groups[fp]), min(_age_rank(i) for i in groups[fp])))
    odd = [iid for iid, fp in fingerprints.items() if fp != reference]
    return reference, odd, len(groups[reference])


async def _read_markers(service: "ClusterService") -> dict:
    """Marker listing through the service's storage probe (one storage job at a
    time, bounded); plain thread for services without a probe (test fakes)."""
    reader = getattr(service, "read_storage_markers", None)
    if callable(reader):
        return await reader(timeout=CHECK_TIMEOUT_S)
    return await _bounded(asyncio.to_thread(service.storage_markers))


async def run_checks(service: "ClusterService") -> list[Check]:
    checks: list[Check] = []
    cluster = service.is_cluster

    def add(check_id: str, status: str, message: str, hint: Optional[str] = None, *, recheck: bool = False) -> None:
        checks.append(Check(check_id, status, message, hint, recheck))

    # The view every membership check works on. A failed or slow refresh is a
    # transient event (VIP move, Galera hiccup): keep the last known view —
    # judging "only this instance is alive" from an empty list would be wrong —
    # and say so with a warning, never with a readiness failure.
    refresh_error: Optional[str] = None
    try:
        instances = await _bounded(service.refresh_instances())
    except Exception as exc:
        refresh_error = _why(exc)
        instances = list(getattr(service, "_instances", None) or [])
    alive = [i for i in instances if i.alive]

    # 1. coordinator — live state. /api/ready does not read this result.
    if not cluster:
        add("coordinator", "skip", "single-instance mode — coordinator driver 'local'")
    elif not service.started:
        add("coordinator", "fail", f"coordinator not started: {service.start_error or 'unknown error'}",
            "Check DB connectivity; the cluster_* tables are created automatically on first start.")
    elif refresh_error is not None:
        add("coordinator", "warn",
            f"instance list could not be refreshed ({refresh_error}) — showing the last known view",
            "A single failure is harmless (database fail-over, short network blip). Readiness follows the "
            "heartbeat: /api/ready turns 503 only when no heartbeat succeeded for a full lease TTL.",
            recheck=True)
    elif getattr(service, "heartbeat_stale", False):
        add("coordinator", "warn", "no successful heartbeat within the lease TTL — peers see this instance as dead",
            "Check DB connectivity of this instance (GET /api/cluster → coordinator.heartbeat); "
            "/api/ready answers 503 'heartbeat' until a beat succeeds.", recheck=True)
    else:
        add("coordinator", "ok", f"db coordinator connected, {len(alive)} live instance(s)")

    # 2. redundancy
    if cluster:
        if len(alive) >= 2:
            add("redundancy", "ok", f"{len(alive)} live instances")
        else:
            add("redundancy", "warn", "only this instance is alive — no redundancy yet",
                "Start a second core instance with the same DB, Vault and STORAGE_SECRET.")
    else:
        add("redundancy", "skip", "single-instance mode")

    # 3. STORAGE_SECRET identical. Only the instance(s) whose secret differs from
    #    the cluster's are unfit for traffic: a single odd joiner must not turn
    #    every healthy instance unready (that was a cluster-wide outage).
    if not cluster:
        add("storage_secret", "skip", "single-instance mode")
    else:
        own_fp = getattr(service, "secret_fingerprint", None) or next(
            (i.meta.get("secret_fp") for i in alive if i.is_self), None)
        reference, odd, agreeing = _secret_reference(alive, own_fp)
        if reference is None or not odd:
            add("storage_secret", "ok", "STORAGE_SECRET fingerprint identical on all live instances")
        elif own_fp and own_fp != reference:
            add("storage_secret", "fail",
                f"this instance's STORAGE_SECRET differs from the one {agreeing} of "
                f"{len(alive)} live instance(s) share",
                "Sessions and SSE tickets minted here are rejected by the other instances, so this instance "
                "stays out of rotation. Set the cluster's STORAGE_SECRET here; rotating the secret needs a "
                "restart of ALL instances with the new value (a rolling update keeps the new ones unready).")
        else:
            add("storage_secret", "warn",
                f"STORAGE_SECRET of {', '.join(sorted(odd))} differs from the cluster's",
                "Those instances keep themselves out of rotation (their /api/ready is 503); sessions and SSE "
                "tickets they mint are rejected here. Set the same STORAGE_SECRET on them.")

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
            "The plugin is installed but cannot run anywhere: start an instance whose LYNDRIX_INSTANCE_ROLES "
            "include that role (the IaC Orchestrator needs a Docker host), or uninstall the plugin on this "
            "deployment if it does not belong here. Traffic is not affected — every instance answers 503 "
            "'remote' for that plugin's routes.")
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
        info = await _bounded(service.coordinator.server_info())
        if info.get("wsrep_on") is True and not settings.DB_ALLOW_DIRECT_NODE:
            add("database", "warn", "DB_HOST is a Galera node (wsrep_on=ON) addressed directly",
                "Route all writes through one MaxScale/ProxySQL endpoint (single writer); set DB_ALLOW_DIRECT_NODE=1 to silence.")
        elif info.get("error"):
            add("database", "warn", f"could not inspect the database server: {info['error']}", recheck=True)
        else:
            extra = " (snapshot isolation on → coordinator uses READ COMMITTED)" if info.get("snapshot_isolation") else ""
            add("database", "ok", f"{info.get('dialect', 'db')} {info.get('version', '')}{extra}".strip())
    except Exception as exc:
        add("database", "warn", f"database inspection failed: {_why(exc)}", recheck=True)

    # 10. clock drift vs. the DB clock (leases are compared on the DB clock)
    try:
        db_now = await _bounded(service.coordinator.db_now())
        local = datetime.now(timezone.utc).replace(tzinfo=None)
        drift = abs((local - db_now).total_seconds())
        if drift > _CLOCK_DRIFT_WARN_S:
            add("clock", "warn", f"this instance's clock is {drift:.1f}s off the database clock",
                "Leases use the DB clock, so this is harmless for coordination, but log timestamps and TTL displays will disagree — fix NTP/chrony.")
        else:
            add("clock", "ok", f"clock drift to the database: {drift:.1f}s")
    except Exception as exc:
        add("clock", "skip", f"clock drift check unavailable: {_why(exc)}", recheck=True)

    # 11. Vault nodes
    if settings.VAULT_NODES:
        try:
            results = await _bounded(
                asyncio.gather(*(asyncio.to_thread(_probe_vault, node) for node in settings.vault_nodes)))
        except Exception as exc:
            add("vault_nodes", "warn", f"Vault node probes did not finish: {_why(exc)}", recheck=True)
        else:
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

    # 13. shared storage: every instance drops a marker file in STORAGE_DIR/.cluster on
    #     each heartbeat. Seeing the other live instances' fresh markers proves the
    #     directory is one shared mount (NFS/CIFS/RWX volume); otherwise files written
    #     here are invisible elsewhere.
    if not cluster:
        add("storage", "skip", "single-instance mode — local storage")
    else:
        others = [i for i in alive if not i.is_self]
        markers = {}
        try:
            markers = await _read_markers(service)
        except StorageBusy as exc:
            # The mount is holding an earlier job hostage: do not send a second
            # thread after it, and do not guess — report what is known.
            add("storage", "warn",
                f"{settings.STORAGE_DIR} is not responding — a storage job ({exc.label or 'io'}) has been stuck "
                f"for {exc.stuck_for_s:.0f}s",
                "The mount (NFS/CIFS/RWX volume) hangs. Requests that read or write files block until it is "
                "back; everything else keeps working, so the instance stays in rotation.", recheck=True)
        except Exception as exc:
            add("storage", "warn", f"could not inspect {settings.STORAGE_DIR}: {_why(exc)}", recheck=True)
        else:
            fresh_s = max(3 * service.heartbeat_s, 60)
            if not others:
                add("storage", "warn", f"cannot verify that {settings.STORAGE_DIR} is shared — only this instance is alive",
                    "Uploads and attachments are only safe in a cluster when every instance mounts the same directory (NFS/CIFS/RWX volume); the check turns ok once a second instance reports.")
            else:
                missing = [i.instance_id for i in others if markers.get(i.instance_id, float("inf")) > fresh_s]
                if missing:
                    add("storage", "warn", f"{settings.STORAGE_DIR} is NOT shared — markers of {', '.join(missing)} are not visible here",
                        "Attachments/uploads written on one instance are invisible on the others. Mount one directory (NFS/CIFS/RWX volume) on every instance; an object-store driver (S3/MinIO) is on the roadmap.")
                else:
                    add("storage", "ok", f"{settings.STORAGE_DIR} is a shared mount — markers of {len(others)} peer(s) visible")

    return checks
