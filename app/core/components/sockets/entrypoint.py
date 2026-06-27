from core.components.plugins.logic.models import ModuleManifest

from .providers.docker_provider import DockerProvider
from .registry import get_registry

manifest = ModuleManifest(
    id="lyndrix.core.sockets",
    name="Socket Manager",
    version="1.0.0",
    description="Core socket provider registry and mount resolution services.",
    author="Lyndrix",
    icon="cable",
    type="CORE",
    permissions={
        "subscribe": ["socket:request", "socket:provider:register"],
        "emit": ["socket:response"],
    },
)


def setup(ctx):
    registry = get_registry()
    registry.register(DockerProvider, plugin_name="core")

    @ctx.subscribe("socket:provider:register")
    async def _register_provider(payload):
        provider_class = payload.get("provider_class")
        plugin_name = payload.get("plugin_name") or payload.get("provider_name") or "plugin"
        if provider_class is None:
            ctx.log.warning("Socket provider registration missing provider_class")
            return
        registry.register(provider_class, plugin_name=plugin_name)

    @ctx.subscribe("socket:request")
    async def _handle_request(payload):
        request_id = payload.get("request_id")
        operation = (payload.get("operation") or "").strip()
        provider_name = (payload.get("provider") or "docker").strip()
        args = payload.get("args") or {}
        provider = registry.get_provider(provider_name)

        # Guard against a missing/None request_id — slicing it raw would raise a
        # TypeError before the error-capturing try-block below.
        rid_short = str(request_id or "")[:8]

        import time
        start_time = time.monotonic()
        ctx.log.debug(f"[socket:request] {operation} from {provider_name} (request_id={rid_short}...)")

        response = {
            "request_id": request_id,
            "operation": operation,
            "provider": provider_name,
            "ok": False,
            "result": None,
            "error": None,
        }

        try:
            if provider is None:
                raise RuntimeError(f"Unknown socket provider '{provider_name}'")

            if operation == "provider:list":
                response["ok"] = True
                response["result"] = {
                    "providers": registry.list_all(),
                    "available": list(registry.list_available().keys()),
                }
            elif operation == "docker:health":
                response["ok"] = True
                response["result"] = await provider.health_check()
            elif operation == "docker:mounts":
                response["ok"] = True
                response["result"] = await provider.get_mounts()
            elif operation == "docker:storage_root":
                response["ok"] = True
                response["result"] = {"storage_root": await provider.detect_storage_root()}
            elif operation == "docker:resolve_mounts":
                response["ok"] = True
                response["result"] = {
                    "mounts": await provider.resolve_current_mounts(args.get("required_targets"))
                }
            elif operation == "docker:cleanup":
                count, error = await provider.cleanup_stale(
                    prefix=args.get("prefix", "aac-runner-"),
                    max_age_minutes=int(args.get("max_age_minutes", 60)),
                )
                response["ok"] = error is None
                response["result"] = {"removed": count, "error": error}
                response["error"] = error
            elif operation == "docker:runners":
                response["ok"] = True
                response["result"] = {
                    "containers": await provider.list_containers(prefix=args.get("prefix", "aac-runner-"))
                }
            elif operation == "docker:spawn":
                spawn = await provider.spawn_runner(
                    image=args["image"],
                    name=args["name"],
                    env_vars=args.get("env_vars") or [],
                    mounts=args.get("mounts") or [],
                    command=args.get("command"),
                    remove=bool(args.get("remove", True)),
                    networks=args.get("networks"),
                )
                response["ok"] = spawn.status == "running"
                response["result"] = spawn.to_dict()
                response["error"] = spawn.error
            else:
                raise RuntimeError(f"Unsupported socket operation '{operation}'")
        except Exception as exc:
            response["error"] = str(exc)

        elapsed = time.monotonic() - start_time
        ctx.log.info(f"[socket:response] {operation} completed in {elapsed:.2f}s (ok={response['ok']}, request_id={rid_short}...)")
        ctx.emit("socket:response", response)


def teardown(ctx):
    return None
