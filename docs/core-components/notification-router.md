# Notification Router Component

## Purpose

The Notification Router owns the routing pipeline between plugin code and the two delivery channels: the in-app notification service (toasts + history) and the external [Messaging Gateway](messaging.md). Plugins declare named **notification endpoints** in their manifest and emit them through `ctx.notify(...)`; operators then decide — per endpoint — whether each one is delivered internally, externally, or both.

This decouples *what* a plugin wants to announce from *whether and where* it is actually delivered.

## Main locations

- `app/core/components/notification_router/entrypoint.py` — CORE component wiring
- `app/core/components/notification_router/logic/router_service.py` — `NotificationRouterService`, envelope handling, discovery sync
- `app/core/components/notification_router/logic/precedence.py` — env > DB > default resolver
- `app/core/components/notification_router/logic/endpoint_registry.py` — in-memory endpoint registry
- `app/core/components/notification_router/models.py` — `NotificationEnvelope`, `ResolvedState`
- `app/core/components/notification_router/ui/notifications_settings.py` — operator settings UI

## Responsibilities

- discover every plugin-declared `NotificationEndpoint` and keep a registry in sync with the live manifests
- persist per-endpoint binding state (`plugin_notification_endpoints` table) and prune rows for endpoints that disappeared
- validate each incoming `NotificationEnvelope` against the registry (unknown endpoint → warning + drop)
- resolve the effective active state and provider via **env var > DB row > manifest default** precedence
- apply internal effects (toast / persisted history) through the notification service
- dispatch externally through the messaging gateway when a registered provider is bound
- support sticky/updatable notifications and `clear` semantics by `notification_id`

## Events

### Subscribes

- `db:connected` — hydrate the precedence cache from the DB and run the discovery sync
- `notification:routed` — a `NotificationEnvelope`; the routing pipeline runs on it
- `ui:needs_refresh` — re-sync declared endpoints (e.g. after a plugin loads/unloads)

### Emits

- `messaging:outbound` — when an endpoint resolves to a registered external provider
- `ui:needs_refresh`

## Declaring and emitting notifications (plugins)

Declare endpoints in the manifest, then emit through the context helper:

```python
from core.api import ModuleManifest, NotificationEndpoint

manifest = ModuleManifest(
    id="lyndrix.plugin.deployer",
    name="Deployer",
    version="0.1.0",
    type="PLUGIN",
    notification_endpoints=[
        NotificationEndpoint(
            name="deployment_succeeded",          # snake_case, [a-z][a-z0-9_]*
            description="A deployment finished successfully.",
            default_active=True,
            internal_toast=True,      # raise an in-app toast
            internal_persist=True,    # append to notification history
            external_default=False,   # route to the global default provider when no binding exists
        ),
    ],
)

def setup(ctx):
    ...

async def on_deploy_done(ctx):
    ctx.notify(
        "deployment_succeeded",
        title="Deploy complete",
        body="prod-web-01 is live",
        severity="success",
    )
```

`ctx.notify()` raises `PermissionError` if `endpoint_name` is not declared in the manifest. It builds a `NotificationEnvelope` and emits it on `notification:routed`; the router takes over from there.

## Operator configuration and precedence

For each `(plugin_id, endpoint_name)` the **active** flag and **provider** binding resolve in this order:

1. **Environment variable** (highest priority, locks the value in the UI)
   - `LYNDRIX_NOTIF__<PLUGIN_ID>__<ENDPOINT_NAME>__ACTIVE`
   - `LYNDRIX_NOTIF__<PLUGIN_ID>__<ENDPOINT_NAME>__PROVIDER`
   - `<PLUGIN_ID>` is upper-cased with `.` and `-` replaced by `_` (e.g. `lyndrix.plugin.deployer` → `LYNDRIX_PLUGIN_DEPLOYER`).
2. **DB row** — set from the Notifications settings UI.
3. **Manifest default** — `default_active`, and for the provider the global default `LYNDRIX_NOTIF_DEFAULT_PROVIDER` (only when the endpoint sets `external_default=True`).

`ResolvedState` reports both the effective value and its `active_source` / `provider_source` so the UI can show where a setting comes from.

## Persistence

The `plugin_notification_endpoints` table (created on `db:connected`) stores one row per `(plugin_id, endpoint_name)` with `is_active`, `provider_binding`, and the declared defaults. On every discovery sync, current manifest declarations are upserted and rows whose declaration disappeared (plugin uninstalled or endpoint renamed) are deleted in one transaction.

## Integration notes

- Internal effects are applied via `notification_service._process_notification(...)` directly to avoid re-emitting `notification:outbound` (which the gateway would also bridge — that would double-deliver).
- External dispatch only happens when the resolved provider is actually registered in the gateway; otherwise the router logs a warning and skips external delivery.
- Stable plugin surface from `core.api`: `NotificationEndpoint`, `NotificationEnvelope`, `ResolvedState`, plus `ctx.notify(...)` on the `ModuleContext`.
