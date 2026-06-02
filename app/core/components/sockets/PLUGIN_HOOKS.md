"""
Socket Provider Registration Hook for Plugins

Plugins can register custom socket providers to enable new socket-based functionality
(DHCP, DNS, system services, etc.) while maintaining security through core auth/permissions.

Example: KEA DHCP Plugin
========================

File: plugin.py
---------------
from core.components.sockets.logic.base_socket_provider import BaseSocketProvider, ProviderCapabilities
from core.components.sockets.registry import get_registry


class KeaProvider(BaseSocketProvider):
    '''KEA DHCP socket provider for lease management, configuration, etc.'''

    @property
    def name(self) -> str:
        return "kea-dhcp"

    @property
    def socket_path(self) -> str:
        return "/var/run/kea/kea4.sock"  # Unix socket path

    @property
    def is_available(self) -> bool:
        from pathlib import Path
        return Path(self.socket_path).exists()

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            can_spawn=False,  # KEA doesn't spawn containers
            can_cleanup=False,
            can_query_mounts=False,
            can_verify_mounts=True,  # Can verify lease files exist
            can_repair_permissions=True,  # Can fix lease file permissions
        )

    async def health_check(self) -> Dict:
        # Check KEA daemon health
        pass

    async def cleanup_stale(self, prefix: str, max_age_minutes: int) -> Tuple[int, Optional[str]]:
        # Clean up old leases
        pass

    async def get_mounts(self) -> Dict[str, Dict[str, str]]:
        # Return {}  - KEA doesn't have container mounts
        return {}

    async def detect_storage_root(self) -> Optional[str]:
        # Return None - KEA doesn't have a storage root
        return None


async def on_load(ctx):
    '''Called when plugin is loaded by core.'''
    registry = get_registry()
    registry.register(KeaProvider, plugin_name="kea-dhcp")


File: manifest.json
--------------------
{
  "name": "kea-dhcp",
  "version": "0.1.0",
  "provides": ["socket_providers"],
  "socket_providers": [
    "kea-dhcp"
  ]
}


API Usage
=========

1. List all registered providers:
   GET /api/socket/providers
   
   Response:
   {
     "providers": {
       "docker": {
         "socket_path": "/var/run/docker.sock",
         "available": true,
         "capabilities": {...}
       },
       "kea-dhcp": {
         "socket_path": "/var/run/kea/kea4.sock",
         "available": true,
         "capabilities": {
           "can_verify_mounts": true,
           "can_repair_permissions": true,
           ...
         }
       }
     },
     "available": ["docker", "kea-dhcp"],
     "total": 2
   }

2. Query a provider's health:
   GET /api/socket/kea-dhcp/health
   (Endpoint auto-registered for any provider with can_query_mounts)

3. Repair permissions on KEA socket:
   POST /api/socket/repair
   Body: {
     "target_dir": "/var/run/kea",
     "user": "kea",
     "mode": 755
   }
   (Requires permission: socket:mounts:repair)


Security Model
==============

1. Plugins declare socket providers in manifest.json under "socket_providers"
2. On plugin load, core validates declared providers match what's being registered
3. All socket access via /api/socket/* is guarded by @require_permission()
4. Plugins cannot:
   - Register providers not declared in manifest
   - Access other plugins' providers directly
   - Bypass core auth/permission system
5. Only core can register providers; plugins register via hooks


Future Extensions
=================

- Systemd socket provider (for systemd services)
- UNIX socket provider (generic socket communication)
- Named pipe provider (Windows)
- gRPC socket provider (gRPC services)
"""
