import asyncio
from collections import defaultdict
from datetime import datetime, timezone
import threading
from uuid import UUID, uuid4

from fastapi import APIRouter
from core.bus import bus as global_bus
from core.logger import get_logger
from core.services import vault_instance
from .models import ModuleManifest

# Runtime phases a module may report via ``ctx.set_status``. ``blocking`` only
# has an effect in the two transient phases.
RUNTIME_PHASES = ("starting", "ready", "degraded", "maintenance")
RUNTIME_BLOCKING_PHASES = ("starting", "maintenance")
RUNTIME_STATUS_TOPIC = "plugin:runtime_status"
# Progress-only updates are coalesced to this interval so a busy warm-up loop
# cannot flood the bus / SSE clients. Phase changes are always emitted.
_RUNTIME_EMIT_MIN_INTERVAL_S = 1.0


def _ready_runtime(now: datetime | None = None) -> dict:
    ts = (now or datetime.now(timezone.utc)).isoformat()
    return {
        "phase": "ready",
        "message": None,
        "progress": None,
        "blocking": False,
        "since": ts,
        "updated_at": ts,
        "retry_after": None,
    }


class _MaintenanceWindow:
    """``async with ctx.maintenance("…")`` / ``with ctx.maintenance("…")``."""

    def __init__(self, ctx: "ModuleContext", message: str | None, blocking: bool, retry_after: int | None):
        self._ctx = ctx
        self._message = message
        self._blocking = blocking
        self._retry_after = retry_after

    def __enter__(self):
        self._ctx.set_status("maintenance", message=self._message, blocking=self._blocking,
                             retry_after=self._retry_after)
        return self._ctx

    def __exit__(self, exc_type, exc, tb):
        self._ctx.set_status("ready")
        return False  # never swallow the exception

    async def __aenter__(self):
        return self.__enter__()

    async def __aexit__(self, exc_type, exc, tb):
        return self.__exit__(exc_type, exc, tb)


class _LeaseScope:
    """``async with ctx.lease("name") as held:`` — one-shot, non-renewed lease.

    If the module already holds the lease through ``ctx.on_lease`` the scope
    reports ``held=True`` without touching the coordinator (and does not
    release it on exit). Otherwise it tries to acquire the lease once for
    ``ttl`` seconds and releases it when the block ends.
    """

    def __init__(self, ctx: "ModuleContext", name: str, ttl: int | None):
        self._ctx = ctx
        self._name = name
        self._ttl = ttl
        self._acquired_here = False
        self.held = False

    async def __aenter__(self) -> bool:
        cluster = self._ctx._cluster()
        if cluster.leases.holds(self._name):
            self.held = True
            return True
        try:
            self.held = bool(await cluster.coordinator.try_acquire(self._name, self._ttl))
        except Exception as exc:
            self._ctx.log.debug("LEASE: try_acquire('%s') failed: %s", self._name, exc)
            self.held = False
        self._acquired_here = self.held
        return self.held

    async def __aexit__(self, exc_type, exc, tb):
        if self._acquired_here:
            try:
                await self._ctx._cluster().coordinator.release(self._name)
            except Exception as rel_exc:
                self._ctx.log.debug("LEASE: release('%s') failed: %s", self._name, rel_exc)
        return False


class ModuleContext:
    """
    Der isolierte Sandkasten für JEDES Modul (Core & Plugin).
    """
    _vault_write_locks = defaultdict(threading.RLock)

    def __init__(self, manifest: ModuleManifest):
        self.manifest = manifest
        # Logger zeigt z.B. [Core: IAM] oder [Plugin: Discord]
        prefix = "Core" if manifest.type == "CORE" else "Plugin"
        self.log = get_logger(f"{prefix}:{manifest.name}")
        self.state = {}
        # Track this module's bus subscriptions and background tasks so the
        # ModuleManager can tear them down on unload/disable/reload — otherwise a
        # reloaded plugin keeps firing its old handlers and leaks a task per cycle.
        self._subscriptions: list[tuple[str, object]] = []
        self._tasks: list = []
        # Leases registered via on_lease(): (lease name, acquired cb, lost cb).
        self._leases: list[tuple[str, object, object]] = []
        # request()/reply plumbing: one listener per reply topic, futures keyed
        # by request_id.
        self._pending_requests: dict[str, asyncio.Future] = {}
        self._reply_listeners: set[str] = set()
        # Runtime status (startup sweep / maintenance window), see set_status().
        self._runtime: dict = _ready_runtime()
        self._runtime_emitted_at: datetime | None = None

    # --- RUNTIME STATUS (startup sequence / maintenance window) ---

    @property
    def runtime(self) -> dict:
        """Current runtime status: ``{phase, message, progress, blocking, since,
        updated_at, retry_after}``. Always present — a module that never calls
        ``set_status`` is ``ready``."""
        return dict(self._runtime)

    def set_status(
        self,
        phase: str,
        *,
        message: str | None = None,
        progress: tuple[int, int] | None = None,
        blocking: bool = False,
        retry_after: int | None = None,
    ) -> dict:
        """Report this module's runtime phase to the core.

        ``phase``: ``starting`` (warm-up such as an initial probe sweep),
        ``ready``, ``degraded`` (working with reduced function) or
        ``maintenance`` (temporarily unavailable on purpose).

        ``progress=(done, total)`` is shown as a progress bar. ``blocking=True``
        (only meaningful for ``starting``/``maintenance``) tells the core to
        answer this module's API routes with ``503 Retry-After`` and the React
        shell to show a warm-up panel instead of the plugin UI; non-blocking
        phases only add a banner. ``retry_after`` (seconds) is sent with the 503.

        The status is kept on the context (reset to ``ready`` on dispose) and
        broadcast as ``plugin:runtime_status`` on the global bus — a core-owned
        topic, so no manifest permission is needed. Progress-only updates are
        coalesced to one per second; phase changes are always emitted.

        Example::

            ctx.set_status("starting", message="Initial probe sweep",
                           progress=(0, total), blocking=True)
            for i, item in enumerate(items, 1):
                ...
                ctx.set_status("starting", message="Initial probe sweep",
                               progress=(i, total), blocking=True)
            ctx.set_status("ready")
        """
        if phase not in RUNTIME_PHASES:
            raise ValueError(f"Unknown runtime phase '{phase}' (expected one of {', '.join(RUNTIME_PHASES)})")
        prog = None
        if progress is not None:
            done, total = progress
            prog = {"done": max(0, int(done)), "total": max(0, int(total))}
        now = datetime.now(timezone.utc)
        prev = self._runtime
        is_blocking = bool(blocking) and phase in RUNTIME_BLOCKING_PHASES
        phase_changed = prev["phase"] != phase or prev["blocking"] != is_blocking
        runtime = {
            "phase": phase,
            "message": message,
            "progress": prog,
            "blocking": is_blocking,
            "since": now.isoformat() if phase_changed else prev["since"],
            "updated_at": now.isoformat(),
            "retry_after": int(retry_after) if retry_after else None,
        }
        self._runtime = runtime

        last = self._runtime_emitted_at
        if not phase_changed and last is not None and (now - last).total_seconds() < _RUNTIME_EMIT_MIN_INTERVAL_S:
            return dict(runtime)  # coalesced; the state is current, the bus is not spammed
        self._emit_runtime(now)
        if phase_changed:
            self.log.info(
                "RUNTIME: %s%s%s", phase, " (blocking)" if is_blocking else "",
                f" — {message}" if message else "",
            )
        return dict(runtime)

    def maintenance(self, message: str | None = None, *, blocking: bool = True,
                    retry_after: int | None = None) -> _MaintenanceWindow:
        """Context manager (sync or async) that reports ``maintenance`` on enter
        and ``ready`` on exit — also when the body raises::

            async with ctx.maintenance("Re-importing inventory"):
                await asyncio.to_thread(self._reimport)
        """
        return _MaintenanceWindow(self, message, blocking, retry_after)

    def reset_runtime(self) -> None:
        """Back to ``ready`` (used by the ModuleManager on dispose/failure) so a
        stopped module can never leave a stale blocking status behind."""
        if self._runtime["phase"] == "ready" and not self._runtime["blocking"]:
            return
        self._runtime = _ready_runtime()
        self._emit_runtime(datetime.now(timezone.utc))

    def _emit_runtime(self, now: datetime) -> None:
        self._runtime_emitted_at = now
        try:
            global_bus.emit(RUNTIME_STATUS_TOPIC, {
                "plugin_id": self.manifest.id, "instance_id": self.instance_id, **self._runtime,
            })
        except Exception:
            self.log.debug("RUNTIME: status emit failed", exc_info=True)

    # --- CLUSTER (core API >= 1.7, docs/ha/plugin-development.md) ---

    @staticmethod
    def _cluster():
        # Lazy: the cluster service imports config/bus only, but keeping it out of
        # the module import keeps the context importable in isolation (tests).
        from core.components.cluster.logic.cluster_service import cluster_service

        return cluster_service

    @property
    def instance_id(self) -> str:
        """Id of the core instance this module runs on (``LYNDRIX_INSTANCE_ID``)."""
        return self._cluster().instance_id

    @property
    def is_leader(self) -> bool:
        """``True`` while this instance holds the core leader lease. Always
        ``True`` in single mode once the DB is connected."""
        return self._cluster().is_leader

    @property
    def cluster_mode(self) -> str:
        """``single`` or ``cluster`` (``LYNDRIX_CLUSTER_MODE``)."""
        return self._cluster().mode

    @property
    def cluster_size(self) -> int:
        """Number of live core instances (1 in single mode)."""
        alive = [i for i in self._cluster()._instances if i.alive]
        return max(1, len(alive))

    @property
    def roles(self) -> list[str]:
        """Roles of this instance (``LYNDRIX_INSTANCE_ROLES``)."""
        return list(self._cluster().roles)

    @property
    def cluster_instances(self) -> list[dict]:
        """Every known core instance as ``{"instance_id", "hostname", "roles",
        "alive", "is_self"}`` (core API ≥ 1.7.1). Lets a plugin whose work is
        split by role (API everywhere, execution on ``runner`` instances) tell
        the operator that no executor is online instead of queueing silently.
        In single mode: a one-element list describing this instance."""
        svc = self._cluster()
        out = [
            {
                "instance_id": i.instance_id,
                "hostname": getattr(i, "hostname", None),
                "roles": list(getattr(i, "roles", None) or []),
                "alive": bool(getattr(i, "alive", False)),
                "is_self": bool(getattr(i, "is_self", False)),
            }
            for i in (getattr(svc, "_instances", None) or [])
        ]
        if not out:
            out.append({"instance_id": svc.instance_id, "hostname": None,
                        "roles": list(svc.roles), "alive": True, "is_self": True})
        return out

    def has_live_role(self, role: str) -> bool:
        """``True`` when at least one live instance (this one included) has ``role``."""
        return any(role in i["roles"] for i in self.cluster_instances if i["alive"])

    def _lease_name(self, name: str) -> str:
        # A singleton plugin is one unit of background work: every on_lease()
        # call binds to the plugin's single lease, whatever name it passes.
        if getattr(self.manifest, "placement", "any") == "singleton":
            return f"plugin:{self.manifest.id}:singleton"
        return name

    def on_lease(self, name: str, *, acquired=None, lost=None, ttl: int | None = None) -> None:
        """Run background work on exactly one instance.

        ``acquired`` fires when this instance obtains lease ``name`` (immediately
        in single mode), ``lost`` when a peer took it over or the coordinator was
        unreachable for a full TTL — stop the work in ``lost``. Callbacks may be
        sync or async. Example::

            def setup(ctx):
                ctx.register_routes(router)          # every instance
                ctx.on_lease("monitoring:prober",     # one instance
                             acquired=svc.start_probing, lost=svc.stop_probing)

        Guard with ``getattr(ctx, "on_lease", None)`` to stay loadable on older
        cores (fall back to starting the work directly).
        """
        full = self._lease_name(name)
        self._cluster().leases.want(full, acquired=acquired, lost=lost, ttl=ttl)
        self._leases.append((full, acquired, lost))
        self.log.debug("LEASE: wants '%s'", full)

    def holds_lease(self, name: str) -> bool:
        return self._cluster().leases.holds(self._lease_name(name))

    def lease(self, name: str, *, ttl: int | None = None) -> _LeaseScope:
        """One-shot lease for a piece of work (``async with ctx.lease("x") as held``)."""
        return _LeaseScope(self, self._lease_name(name), ttl)

    # --- WORK ASSIGNMENT / LOAD BALANCING (core API >= 1.8) ---
    # Ask the cluster which instances run a named workload and which share of a
    # key set is mine. Every instance computes the same answer locally (rendezvous
    # hashing over the live instance list), so no round trip and only the keys of
    # a leaving/joining instance move. Recompute on ``cluster:changed``. At-least-
    # once while heartbeat views differ — use a lease for exactly-once singletons.

    def cluster_workers(self, name: str, count: int = 1, roles: list[str] | None = None) -> list[str]:
        """The ``count`` live instances (optionally with one of ``roles``) that run ``name``."""
        return self._cluster().assigner.workers(f"{self.manifest.id}:{name}", count, roles)

    def is_worker(self, name: str, count: int = 1, roles: list[str] | None = None) -> bool:
        """Is this instance one of the ``count`` workers of ``name``?"""
        return self._cluster().assigner.is_worker(f"{self.manifest.id}:{name}", count, roles)

    def cluster_shard(self, name: str, keys, count: int | None = None,
                      roles: list[str] | None = None) -> dict[str, list]:
        """Distribute ``keys`` over the workers of ``name`` → ``{instance_id: [keys]}``
        (``count=None`` = every candidate)."""
        return self._cluster().assigner.shard(f"{self.manifest.id}:{name}", keys, count, roles)

    def my_shard(self, name: str, keys, count: int | None = None, roles: list[str] | None = None) -> list:
        """The keys of ``keys`` this instance is responsible for. Example::

            @ctx.subscribe("cluster:changed")
            async def _rebalance(_):
                svc.apply_assignment(ctx.my_shard("probe", svc.monitor_ids(), count=2, roles=["runner"]))
        """
        return self._cluster().assigner.my_shard(f"{self.manifest.id}:{name}", keys, count, roles)

    # --- EVENT BUS PROXY ---

    def subscribe(self, topic: str):
        def decorator(callback):
            if topic in self.manifest.permissions.subscribe or "*" in self.manifest.permissions.subscribe:
                self.log.debug(f"SUBSCRIBE: Subscribed to topic: {topic}")
                global_bus.subscribe(topic)(callback)
                self._subscriptions.append((topic, callback))
            else:
                message = (
                    f"Plugin '{self.manifest.id}' is not permitted to subscribe to "
                    f"'{topic}'. Declare it in permissions.subscribe."
                )
                self.log.error(message)
                raise PermissionError(message)
            return callback
        return decorator

    def emit(self, topic: str, payload: dict = None, *, cluster: bool = False):
        """Emit on the local bus; ``cluster=True`` also reaches every other core
        instance (peers receive it with ``payload["_origin"]`` set). Use it for
        "my data changed, re-read it" signals — never to fan work out N times."""
        if topic in self.manifest.permissions.emit or "*" in self.manifest.permissions.emit:
            global_bus.emit(topic, payload, cluster=cluster)
        else:
            message = (
                f"Plugin '{self.manifest.id}' is not permitted to emit topic "
                f"'{topic}'. Declare it in permissions.emit."
            )
            self.log.error(message)
            raise PermissionError(message)

    async def request(
        self,
        topic: str,
        payload: dict | None = None,
        *,
        reply_topic: str,
        timeout: float = 10.0,
    ) -> dict | None:
        """Emit ``topic`` and await the matching reply on ``reply_topic``.

        Request/response on top of the fire-and-forget bus: a fresh
        ``request_id`` is added to the payload and the responder must echo it
        in its reply. Returns the reply payload, or ``None`` when nobody
        answers within ``timeout`` (responder plugin absent/disabled) — callers
        are expected to degrade gracefully instead of failing.

        Both topics are checked against the manifest: ``topic`` must be in
        ``permissions.emit`` and ``reply_topic`` in ``permissions.subscribe``.
        """
        self._ensure_reply_listener(reply_topic)
        request_id = str(uuid4())
        future = asyncio.get_running_loop().create_future()
        self._pending_requests[request_id] = future
        try:
            self.emit(topic, {**(payload or {}), "request_id": request_id})
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            self.log.debug(f"REQUEST: no reply on '{reply_topic}' for '{topic}' within {timeout}s")
            return None
        finally:
            self._pending_requests.pop(request_id, None)

    def _ensure_reply_listener(self, reply_topic: str) -> None:
        if reply_topic in self._reply_listeners:
            return

        def _on_reply(payload):
            request_id = (payload or {}).get("request_id") if isinstance(payload, dict) else None
            future = self._pending_requests.get(request_id) if request_id else None
            if future is None or future.done():
                return  # someone else's request, or already timed out
            loop = future.get_loop()
            loop.call_soon_threadsafe(
                lambda: future.done() or future.set_result(payload)
            )

        # The bus de-duplicates subscribers by module + qualname; make the
        # listener unique per plugin and topic or the second plugin's
        # listener would be silently dropped as a "duplicate".
        _on_reply.__qualname__ = f"ModuleContext.request<{self.manifest.id}:{reply_topic}>"
        self.subscribe(reply_topic)(_on_reply)
        self._reply_listeners.add(reply_topic)

    def create_task(self, coro, *, name: str = None):
        """Create an observed background task owned by this module."""
        task_name = name or f"module:{self.manifest.id}"
        task = global_bus.create_tracked_task(coro, name=task_name)
        self._tasks.append(task)
        return task

    def dispose(self) -> None:
        """Remove this module's bus subscriptions and cancel its background tasks.

        Called by the ModuleManager on unload/disable so a reloaded or disabled
        plugin's stale handlers stop firing (freeing their de-dup keys for a fresh
        re-subscribe) and its long-lived tasks don't leak. Synchronous by design
        so the sync ``unload_module`` path can call it; ``task.cancel()`` only
        schedules cancellation, which lands at the task's next await point.
        """
        for topic, callback in self._subscriptions:
            try:
                global_bus.unsubscribe(topic, callback)
            except Exception:
                self.log.debug("DISPOSE: unsubscribe failed for '%s'", topic, exc_info=True)
        self._subscriptions.clear()
        self._reply_listeners.clear()
        for future in self._pending_requests.values():
            if not future.done():
                future.cancel()
        self._pending_requests.clear()

        for task in self._tasks:
            try:
                if not task.done():
                    task.cancel()
            except Exception:
                self.log.debug("DISPOSE: task cancel failed", exc_info=True)
        self._tasks.clear()
        # Give leases up so a peer can take the work over right away.
        for name, acquired, lost in self._leases:
            try:
                self._cluster().leases.forget(name, acquired=acquired, lost=lost)
            except Exception:
                self.log.debug("DISPOSE: lease forget failed for '%s'", name, exc_info=True)
        self._leases.clear()
        # A disabled/unloaded module is not "starting" any more — never leave a
        # blocking status behind that would 503 the next incarnation's routes.
        self.reset_runtime()

    def notify(
        self,
        endpoint_name: str,
        payload: dict | None = None,
        *,
        notification_id: str | None = None,
        correlation_id: UUID | None = None,
        title: str | None = None,
        body: str | None = None,
        severity: str = "info",
        clear: bool = False,
    ) -> None:
        """Emit a notification through the central router.

        The endpoint must be declared in this plugin's manifest
        ``notification_endpoints`` list. The router consumes the resulting
        envelope on the ``notification:routed`` topic and applies the user's
        configured routing (internal toast/history and/or external provider).
        """
        declared_names = {ep.name for ep in self.manifest.notification_endpoints}
        if endpoint_name not in declared_names:
            message = (
                f"Plugin '{self.manifest.id}' is not permitted to notify endpoint "
                f"'{endpoint_name}'. Declare it in manifest.notification_endpoints."
            )
            self.log.error(message)
            raise PermissionError(message)

        envelope = {
            "envelope_version": "1",
            "source_plugin_id": self.manifest.id,
            "endpoint_name": endpoint_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "payload": payload or {},
            "notification_id": notification_id,
            "correlation_id": str(correlation_id) if correlation_id else None,
            "title": title,
            "body": body,
            "severity": severity,
            "clear": clear,
        }
        global_bus.emit("notification:routed", envelope)

    def register_gateway_adapter(self, adapter) -> None:
        """Register a GatewayAdapter into the central MessagingGateway.

        Call this in your plugin's ``setup()`` hook after constructing the
        adapter::

            from core.api import GatewayAdapter, GatewayCapability, OutboundMessage

            class MyAdapter(GatewayAdapter):
                provider_id  = "myservice"
                display_name = "My Service"
                capabilities = GatewayCapability.TEXT

                async def send(self, message: OutboundMessage) -> str | None:
                    ...

            def setup(ctx):
                ctx.register_gateway_adapter(MyAdapter(ctx))
        """
        from core.components.messaging.logic.gateway import messaging_gateway
        messaging_gateway.register(adapter)
        self.log.info("GATEWAY: Registered adapter '%s'.", adapter.provider_id)

    # --- HTTP ROUTE REGISTRATION ---

    def register_theme_overrides(self, overrides: dict) -> None:
        """Register partial UIStyles overrides scoped to this plugin.

        Keys must match UIStyles class attribute names (e.g. ``"CARD_BASE"``).
        Overrides are applied only when the plugin's own pages call
        ``resolve_component_styles(plugin_id=self.manifest.id)``.
        They do not affect the global UIStyles class or other plugins.

        Example::

            def setup(ctx):
                ctx.register_theme_overrides({
                    "CARD_BASE": "p-4 rounded-xl border border-zinc-700 bg-zinc-900",
                })
        """
        from core.theming import get_theme_engine
        get_theme_engine().register_plugin_overrides(self.manifest.id, overrides)
        self.log.debug("THEME: Registered %d style override(s).", len(overrides))

    def register_routes(self, router: APIRouter, public: bool = False) -> None:
        """
        Mount a FastAPI ``APIRouter`` for this plugin.

        Routes are prefixed at ``/api/plugins/<module-id>/`` and appear in
        the OpenAPI schema automatically.  Call this inside ``setup(ctx)``.

        By default every route requires authentication (``require_api_auth``)
        at the registry level. Pass ``public=True`` to mount the router
        WITHOUT that dependency — only for routes whose handler performs its
        own authentication, e.g. a webhook that verifies a provider signature
        (Discord/GitLab-style inbound hooks, which must respond before any
        session/API-key check would even apply).

        Example::

            from core.api import APIRouter

            router = APIRouter()

            @router.get("/status")
            def status():
                return {"ok": True}

            def setup(ctx):
                ctx.register_routes(router)
        """
        # Import here to avoid a circular import at module load time.
        from core.api.router_registry import router_registry

        router_registry.register(self.manifest.id, router, public=public)
        kind = "PUBLIC" if public else "authenticated"
        self.log.info("ROUTES: Registered %s HTTP router at /api/plugins/%s/", kind, self.manifest.id)

    # --- VAULT PROXY (Hier war der Einrückungsfehler) ---

    def _get_vault_path(self) -> str:
        """Bestimmt den Basis-Pfad für dieses Modul im Vault."""
        folder = "core" if self.manifest.type == "CORE" else "plugins"
        return f"{folder}/{self.manifest.id}"

    def _get_settings_vault_path(self) -> str:
        """Pfad für schema-driven settings (getrennt von rohen Secrets)."""
        folder = "core" if self.manifest.type == "CORE" else "plugins"
        return f"{folder}/{self.manifest.id}/settings"

    def get_secret(self, key: str) -> str:
        """Lädt einen einzelnen Wert aus dem KV-V2 Store."""
        if not vault_instance.is_connected:
            return None
            
        path = self._get_vault_path()
        try:
            # hvac v2 read: mount_point 'lyndrix' ist das Haupt-Regal
            response = vault_instance.client.secrets.kv.v2.read_secret_version(
                path=path,
                mount_point="lyndrix"
            )
            # In V2 liegen die echten Daten gekapselt in ['data']['data']
            if response and 'data' in response and 'data' in response['data']:
                return response['data']['data'].get(key)
        except Exception:
            return None
        return None

    def get_setting(self, key: str, default=None):
        """Read one value from this plugin's schema-driven settings in Vault.

        Settings are written by the React UI via PUT /api/plugins/{id}/settings
        and stored at a separate Vault path from raw secrets (set_secret).
        Returns *default* when Vault is unreachable or the key has no value.

        Example::

            def setup(ctx):
                @ctx.subscribe("vault:ready_for_data")
                async def _load(_payload=None):
                    ctx.state["timeout"] = ctx.get_setting("timeout_s", default=30)
        """
        from core.components.vault.logic.kv_helper import read_secret_dict

        data = read_secret_dict(self._get_settings_vault_path())
        return data.get(key, default)

    def get_all_settings(self) -> dict:
        """Read all schema-driven settings for this plugin from Vault.

        Returns an empty dict when Vault is unreachable or no settings have
        been saved yet.  Useful for bulk-loading on ``vault:ready_for_data``.

        Example::

            def setup(ctx):
                @ctx.subscribe("vault:ready_for_data")
                async def _load(_payload=None):
                    cfg = ctx.get_all_settings()
                    ctx.state["api_url"] = cfg.get("api_url", "http://localhost")
        """
        from core.components.vault.logic.kv_helper import read_secret_dict

        return read_secret_dict(self._get_settings_vault_path())

    def set_secret(self, key: str, value: str):
        """Speichert einen Wert im KV-V2 Store, ohne andere Keys zu löschen."""
        if not vault_instance.is_connected:
            self.log.error("VAULT: Vault not connected.")
            return False
            
        path = self._get_vault_path()
        try:
            lock = self._vault_write_locks[path]
            with lock:
                # Serialize read-modify-write updates per Vault path so concurrent
                # plugin writes do not overwrite each other.
                current_data = {}
                try:
                    response = vault_instance.client.secrets.kv.v2.read_secret_version(
                        path=path, mount_point="lyndrix"
                    )
                    current_data = response['data']['data']
                except Exception:
                    pass

                current_data[key] = value

                vault_instance.client.secrets.kv.v2.create_or_update_secret(
                    path=path,
                    mount_point="lyndrix",
                    secret=current_data
                )
            self.log.info(f"SUCCESS: Secret '{key}' persisted securely at '{path}'.")
            return True
        except Exception as e:
            self.log.error(f"ERROR: Failed to write to Vault: {e}", exc_info=True)
            return False