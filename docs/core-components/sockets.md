# Sockets Component

## Purpose

The Sockets component is the core registry and access layer for **socket providers** — adapters that mediate privileged, socket-based access to host services (the Docker daemon today; DHCP/DNS/systemd/gRPC in the future). It gives plugins a controlled way to use such sockets without granting them raw access, and centralises mount inspection and permission repair behind core authentication.

This component was previously named `socket_manager`; it was refactored into a bus-driven provider registry under `app/core/components/sockets/`.

## Main locations

- `app/core/components/sockets/entrypoint.py` — CORE component wiring, bus request/response handler
- `app/core/components/sockets/registry.py` — `SocketProviderRegistry`, `get_registry()`
- `app/core/components/sockets/logic/base_socket_provider.py` — `BaseSocketProvider` ABC, `ProviderCapabilities`
- `app/core/components/sockets/logic/mount_guardian.py` — `MountGuardian` (mount health, permission repair)
- `app/core/components/sockets/providers/docker_provider.py` — built-in Docker provider
- `app/core/components/sockets/api/socket_api.py` — permission-guarded HTTP API (`/api/socket/*`)
- `app/core/components/sockets/PLUGIN_HOOKS.md` — provider-registration reference for plugin authors

## Responsibilities

- maintain a registry of socket providers (core + plugin-provided), de-duplicating by provider name
- register the built-in Docker provider before plugins boot
- serve socket operations over the event bus (`socket:request` → `socket:response`)
- expose a permission-guarded HTTP API for provider listing, health, mounts, cleanup, and permission repair
- verify required mount points and repair directory permissions via the Mount Guardian
- enforce that all socket access flows through core auth/permissions — plugins never touch the socket directly

## Events

### Subscribes

- `socket:provider:register` — register a plugin-supplied provider class (payload: `provider_class`, `plugin_name`)
- `socket:request` — perform an operation against a named provider (payload: `request_id`, `operation`, `provider`, `args`)

### Emits

- `socket:response` — the result of a `socket:request` (`ok`, `result`, `error`, …)

Supported `operation` values include `provider:list`, `docker:health`, `docker:mounts`, `docker:storage_root`, `docker:resolve_mounts`, `docker:cleanup`, `docker:runners`, and `docker:spawn`.

## HTTP API

Mounted under `/api/socket` (`tags=["socket-management"]`):

| Method & path | Auth | Purpose |
|---|---|---|
| `GET /api/socket/health?required_dirs=…` | optional | Mount health for the given comma-separated paths (Mount Guardian) |
| `GET /api/socket/providers` | optional | List all registered providers + which are available |
| `GET /api/socket/docker/health` | optional | Docker daemon health |
| `GET /api/socket/docker/mounts` | optional | Container mount mappings |
| `GET /api/socket/docker/storage-root` | optional | Detect the host storage root inside containers |
| `POST /api/socket/docker/cleanup` | `socket:container:cleanup` | Remove stale runner containers |
| `POST /api/socket/repair` | `socket:mounts:repair` | Repair permissions on a target directory |

Mutating endpoints are guarded by `require_permission(...)` (see [Auth](auth.md#api-authentication-and-authorization)); read endpoints use `optional_api_auth`.

## Writing a socket provider (plugins)

Subclass `BaseSocketProvider`, declare its `ProviderCapabilities`, and register it on the global registry. Providers may be registered either directly (`get_registry().register(...)`) or by emitting `socket:provider:register`:

```python
from core.components.sockets.logic.base_socket_provider import (
    BaseSocketProvider, ProviderCapabilities,
)
from core.components.sockets.registry import get_registry

class KeaProvider(BaseSocketProvider):
    @property
    def name(self) -> str: return "kea-dhcp"

    @property
    def socket_path(self) -> str: return "/var/run/kea/kea4.sock"

    @property
    def is_available(self) -> bool:
        from pathlib import Path
        return Path(self.socket_path).exists()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(can_verify_mounts=True, can_repair_permissions=True)

    async def health_check(self) -> dict: ...
    async def cleanup_stale(self, prefix, max_age_minutes): ...
    async def get_mounts(self) -> dict: return {}
    async def detect_storage_root(self): return None

def setup(ctx):
    get_registry().register(KeaProvider, plugin_name="kea-dhcp")
```

The registry validates that the class inherits from `BaseSocketProvider`, instantiates it once, and rejects duplicate provider names.

## Security model

- Plugins declare their socket providers in the manifest; on load, core validates the declared providers against what is actually registered.
- All `/api/socket/*` access is guarded by core authentication; mutating operations require an explicit permission.
- Plugins cannot register undeclared providers, reach another plugin's provider directly, or bypass the core auth/permission system. Only core registers providers; plugins do so through the registry hook.

## Integration notes

- The built-in `docker` provider backs runner-container lifecycle for automation plugins (e.g. the IaC Orchestrator) via the `docker:*` bus operations.
- See `PLUGIN_HOOKS.md` in the component directory for the full provider-authoring reference and planned provider types (systemd, generic UNIX socket, named pipe, gRPC).
