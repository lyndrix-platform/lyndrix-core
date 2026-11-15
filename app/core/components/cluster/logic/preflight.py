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
    elif not service.started and not getattr(service, "start_error", None):
        # Boot completes while the first coordinator start (lease tick + first
        # heartbeat) is still awaiting the DB: that is "starting", not a failure.
        # A FAIL here logged an ERROR on every instance start (staging 2026-11-15).
        # The service re-runs the preflight once it is online.
        add("coordinator", "warn", "coordinator is still starting (first lease pass/heartbeat in progress)",
            "Re-evaluated as soon as the coordinator is online; a start error turns this into a failure.",
            recheck=True)
    elif not service.started:
        add("coordinator", "fail", f"coordinator not started: {service.start_error}",
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

    # 5b. boot-time plugin convergence (readiness waits for it, up to a cap)
    if not cluster:
        add("plugins", "skip", "single-instance mode")
    elif getattr(service, "_plugins_converged", lambda: True)():
        add("plugins", "ok", "plugins recorded for the cluster are installed and activated here")
    elif getattr(service, "plugins_converging", False):
        add("plugins", "warn",
            f"plugins are still being installed/activated ({service.plugin_wait_elapsed_s():.0f}s since boot) — "
            "not ready for traffic yet",
            "A new instance pulls the plugins recorded in plugin_states before it joins the load balancer "
            "(/api/ready: plugins:converging).", recheck=True)
    else:
        add("plugins", "warn",
            f"plugin convergence did not finish within {getattr(service, 'plugin_wait_s', 0):.0f}s — "
            "this instance serves traffic anyway",
            "Routes of plugins that are not installed here answer 404. Check the log for RECONCILE / install "
            "errors (repository unreachable?) and GET /api/plugins on this instance; "
            "LYNDRIX_READY_PLUGIN_WAIT_SECONDS sets the wait.", recheck=True)

    # 6. role coverage
    alive_roles = {r for i in alive for r in i.roles} | set(service.roles)
    pinned = _pinned_roles()
    # A role whose only instance is restarting gracefully is not missing — it
    # is on its way back (planned restart within the grace).
    returning_roles = {r for i in instances if getattr(i, "state", "") == "restarting" for r in i.roles}
    missing = {role: ids for role, ids in pinned.items() if role not in alive_roles}
    returning = {role: ids for role, ids in missing.items() if role in returning_roles}
    if missing and len(returning) == len(missing):
        add("roles", "warn", "role(s) restarting: " + ", ".join(
            f"'{role}' (needed by {', '.join(ids)})" for role, ids in sorted(returning.items())),
            "The instance carrying the role stopped gracefully and is expected back; its plugins answer "
            "503 with a short Retry-After meanwhile.", recheck=True)
    elif missing:
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
    trust_everyone = "*" in {p.strip() for p in proxies.split(",")}
    if trust_everyone:
        # Not a failure (the usual Kubernetes setting, and fine while only the
        # ingress can reach the core) — but the operator has to know what it means.
        add("trusted_proxies", "warn", "LYNDRIX_TRUSTED_PROXIES is '*' — X-Forwarded-For is believed from every sender",
            "Whoever reaches the core port directly (other pods, a published port, a NodePort) can send any "
            "X-Forwarded-For and so choose the client address the login/unseal limiters and the logs see: the "
            "per-address limits are then free to dodge and only the per-account limit holds. Set "
            "LYNDRIX_TRUSTED_PROXIES to the address(es) or CIDR of the load balancer / ingress pods, or make sure "
            "nothing but the load balancer can open a connection to the core (NetworkPolicy, no published port).")
    elif service.saw_forwarded_for and default_proxies:
        add("trusted_proxies", "warn", "X-Forwarded-For arrives but LYNDRIX_TRUSTED_PROXIES is the default",
            "The login rate limiter and audit entries see the proxy's IP, so all clients share one limiter bucket "
            "— set LYNDRIX_TRUSTED_PROXIES to the address(es) or CIDR of the load balancer.")
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

    # 11. Vault as this instance sees it (seal watchdog) and the configured nodes
    try:
        from core.services import vault_instance

        if getattr(vault_instance, "is_connected", False):
            add("vault", "ok", "Vault is unsealed and reachable from this instance")
        else:
            gates = bool(getattr(settings, "LYNDRIX_READY_REQUIRES_VAULT", False))
            add("vault", "warn",
                f"Vault is not available to this instance (state: {getattr(vault_instance, 'ui_state', 'unknown')}) — "
                "secrets and Vault-stored settings cannot be read or written",
                ("/api/ready answers 503 'vault' until it is back (LYNDRIX_READY_REQUIRES_VAULT=true)." if gates else
                 "The instance stays in rotation; the watchdog reconnects by itself once Vault is unsealed and "
                 "reachable. Set LYNDRIX_READY_REQUIRES_VAULT=true to fail readiness instead."),
                recheck=True)
    except Exception as exc:  # pragma: no cover - import-order edge case during tests
        add("vault", "skip", f"Vault state unavailable: {_why(exc)}")
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

    # 13. shared storage: every instance writes a marker file STORAGE_DIR/.cluster/<id>
    #     naming its incarnation on each heartbeat. Reading a peer's marker here with
    #     the incarnation its registry row carries proves that both look at ONE
    #     directory (NFS/CIFS/RWX volume); otherwise files written here are invisible
    #     there. Only instances that serve user files have to share.
    if not cluster:
        add("storage", "skip", "single-instance mode — local storage")
    else:
        await _check_storage(service, alive, add)

    return checks


def _sharing_roles() -> set[str]:
    return {r.strip() for r in (getattr(settings, "LYNDRIX_STORAGE_SHARED_ROLES", "api") or "").split(",") if r.strip()}


def _uptime_s(inst) -> float:
    """Seconds since the instance registered, on the database clock."""
    if isinstance(inst.started_at, datetime) and isinstance(inst.heartbeat_at, datetime):
        return max(0.0, (inst.heartbeat_at - inst.started_at).total_seconds() + inst.age_s)
    return float("inf")  # unknown start → not "young"


def _label(instances: list) -> str:
    return ", ".join(f"{i.instance_id} ({','.join(i.roles) or '-'})" for i in sorted(instances, key=lambda i: i.instance_id))


async def _visible_markers(service: "ClusterService", peers: list) -> set[str]:
    """Ids of ``peers`` whose marker is visible in this instance's STORAGE_DIR."""
    reader = getattr(service, "read_peer_markers", None)
    if callable(reader):
        found = await reader([p.instance_id for p in peers], timeout=CHECK_TIMEOUT_S)
        # "legacy": marker of a core ≤ 1.11 peer — present under its name, no
        # incarnation to compare. A marker of ANOTHER incarnation is a leftover.
        return {p.instance_id for p in peers
                if found.get(p.instance_id) in ("legacy", p.incarnation) and found.get(p.instance_id) is not None}
    # Services without a marker reader (test fakes): the former age heuristic.
    ages = await _bounded(asyncio.to_thread(service.storage_markers))
    fresh_s = max(3 * service.heartbeat_s, 60)
    return {p.instance_id for p in peers if ages.get(p.instance_id, float("inf")) <= fresh_s}


async def _check_storage(service: "ClusterService", alive: list, add) -> None:
    directory = settings.STORAGE_DIR
    sharing = _sharing_roles()
    own_roles = set(service.roles)
    others = [i for i in alive if not i.is_self]
    # A peer that registered a moment ago may not have written its marker yet.
    young = [i for i in others if _uptime_s(i) < 2 * service.heartbeat_s]
    settled = [i for i in others if i not in young]
    try:
        visible = await _visible_markers(service, settled) if settled else set()
    except StorageBusy as exc:
        # The mount is holding an earlier job hostage: do not send a second
        # thread after it, and do not guess — report what is known.
        add("storage", "warn",
            f"{directory} is not responding — a storage job ({exc.label or 'io'}) has been stuck "
            f"for {exc.stuck_for_s:.0f}s",
            "The mount (NFS/CIFS/RWX volume) hangs. Requests that read or write files block until it is "
            "back; everything else keeps working, so the instance stays in rotation.", recheck=True)
        return
    except Exception as exc:
        add("storage", "warn", f"could not inspect {directory}: {_why(exc)}", recheck=True)
        return

    must_share = [i for i in settled if sharing & set(i.roles)]
    not_shared = [i for i in settled if i.instance_id not in visible]
    if not (sharing & own_roles):
        # This instance serves no user files (e.g. a runner on its own host).
        seen = [i for i in settled if i.instance_id in visible]
        add("storage", "ok",
            f"this instance (roles {','.join(sorted(own_roles)) or '-'}) does not have to share {directory}"
            + (f" — it shares it with {_label(seen)}" if seen else " — it has a directory of its own"))
        return
    missing = [i for i in must_share if i.instance_id not in visible]
    if missing:
        add("storage", "warn",
            f"{directory} is NOT shared with {_label(missing)} — the instance(s) mount a different directory",
            "Files written through one of these instances (uploads, attachments, uploaded themes) do not exist "
            "on the other: a user routed to both sees them come and go. Mount the same NFS/CIFS/RWX volume at "
            f"{directory} on every instance that serves users, or keep the odd instance out of the user-facing "
            "load balancer (roles are set with LYNDRIX_INSTANCE_ROLES; LYNDRIX_STORAGE_SHARED_ROLES names the "
            "roles that must share).", recheck=True)
        return
    apart = [i for i in not_shared if i not in must_share]
    note = (f"; not shared with {_label(apart)} — fine, they do not serve user files" if apart else "")
    if must_share:
        add("storage", "ok",
            f"{directory} is a shared mount — markers of {len(must_share)} peer(s) visible{note}")
    elif young:
        add("storage", "warn", f"cannot verify yet that {directory} is shared — {_label(young)} just started",
            "Checked again in a moment.", recheck=True)
    else:
        add("storage", "warn",
            f"cannot verify that {directory} is shared — only this instance is alive with a role that "
            f"serves files ({','.join(sorted(sharing)) or 'none'}){note}",
            "Uploads and attachments are only safe in a cluster when every instance that serves users mounts the "
            "same directory (NFS/CIFS/RWX volume); the check turns ok once a second such instance reports.")
