# Messaging Gateway Component

## Purpose

The Messaging Gateway is the central, two-way messaging registry for Lyndrix Core. Plugins register **provider adapters** (Discord, Telegram, Slack, …) at runtime, and any code can deliver a message to one or all of them by emitting a single event. Inbound interactions (button clicks, replies) are routed back to the originating plugin through a correlation mechanism.

It is the outbound transport used by the [Notification Router](notification-router.md): the router resolves *which* provider a notification should go to, the gateway handles *how* it is delivered.

## Main locations

- `app/core/components/messaging/entrypoint.py` — CORE component wiring
- `app/core/components/messaging/gateway.py` — `MessagingGateway` singleton, dispatch pipeline, `StreamBridge`
- `app/core/components/messaging/adapter.py` — `GatewayAdapter` ABC, `GatewayCapability`, `ProviderConfigField`
- `app/core/components/messaging/models.py` — `OutboundMessage`, `InboundMessage`, `ActionButton`, `DeliveryResult`, `MessageSeverity`
- `app/core/components/messaging/correlation.py` — `CorrelationStore`, `PendingAction` (two-layer cache + DB)
- `app/core/components/messaging/internal_adapter.py` — built-in `system` adapter

## Responsibilities

- maintain the registry of provider adapters (`register` / `unregister` / `list_adapters`)
- route an `OutboundMessage` to a target provider or broadcast to all (`dispatch`)
- bound every `adapter.send()` by a per-adapter timeout and retry failures in the background with exponential backoff
- inject and persist correlation IDs for interactive messages so user responses resume the originating plugin
- resolve inbound provider payloads back to the originating plugin's resume topic (`handle_incoming`)
- provide a streaming bypass (`StreamBridge`) for LLM token streams that must not flood the event bus
- register the always-present built-in `system` adapter before any plugin loads

## Events

### Subscribes

- `db:connected` — initialise the correlation table and start background retry workers
- `messaging:outbound` — payload is an `OutboundMessage`; routed through `dispatch()`

### Emits

- `messaging:action_response` — declared for interactive round-trips
- the resume topic of a pending action (e.g. `<plugin>:action_response`) when an inbound correlation resolves

> The gateway deliberately does **not** subscribe to the legacy `notification:outbound` topic, to avoid duplicate delivery. The Discord adapter keeps its own legacy subscription for backward compatibility; new code should emit `messaging:outbound` (or use `ctx.notify(...)` via the Notification Router).

## Configuration

Set in `app/config.py` (`Settings`):

| Variable | Default | Purpose |
|---|---|---|
| `LYNDRIX_GATEWAY_PROVIDERS` | `""` | Comma-separated provider instances as `type:instance_id` (e.g. `discord:ops,discord:alerts,slack:team`). Multiple instances of the same type are supported. |
| `LYNDRIX_GATEWAY_SEND_TIMEOUT_SECONDS` | `30.0` | Max seconds to wait for one `adapter.send()`. Adapters may override per-class via `send_timeout`. |
| `LYNDRIX_GATEWAY_MAX_RETRY_ATTEMPTS` | `3` | Max background retry attempts for a failed dispatch. |
| `LYNDRIX_GATEWAY_CORRELATION_TTL_SECONDS` | `3600` | TTL for pending interactive actions. |

**Per-instance provider settings** are read from environment variables following the pattern
`LYNDRIX_GATEWAY_{TYPE}_{INSTANCE_ID}_{SETTING}` (instance id upper-cased, `-`/space → `_`). For example, the `discord:ops` instance reads its webhook from `LYNDRIX_GATEWAY_DISCORD_OPS_WEBHOOK_URL`. Use `settings.gateway_provider_specs` to enumerate configured instances and `settings.get_gateway_provider_setting(env_prefix, key)` to read one value (falling back to Vault via `ctx.get_secret()` when unset).

## Writing a provider adapter (plugins)

Subclass `GatewayAdapter`, declare the three class vars, implement `send()`, and register in `setup()`:

```python
from core.api import GatewayAdapter, GatewayCapability, OutboundMessage

class SlackAdapter(GatewayAdapter):
    provider_id  = "slack"            # stable unique id
    display_name = "Slack"            # shown in the Notifications settings UI
    capabilities = GatewayCapability.TEXT | GatewayCapability.INTERACTIVE

    async def send(self, message: OutboundMessage) -> str | None:
        # Deliver the message; return a provider message id or None.
        # Must never raise — log and return None on failure.
        ...

def setup(ctx):
    ctx.register_gateway_adapter(SlackAdapter())
```

- **Capabilities** (`GatewayCapability`) are feature flags (`TEXT`, `RICH_MEDIA`, `FILE_ATTACHMENTS`, `INTERACTIVE`, `THREADS`, `REACTIONS`, `TYPING_INDICATOR`, `EDIT_MESSAGE`, `DELETE_MESSAGE`, `STREAMING`). Capability-gated fields on `OutboundMessage` (e.g. `image_url`, `file_paths`, `thread_id`) are silently ignored by adapters that don't declare the matching flag.
- Override `handle_incoming(raw)` if the adapter declares `INTERACTIVE`; it parses a provider webhook into an `InboundMessage` and is called from the adapter's FastAPI route via `messaging_gateway.handle_incoming(provider_id, raw)`.
- Override `get_config_fields()` / `save_config()` (returning `ProviderConfigField`s) to render inline edit forms in the Notifications settings UI with env-lock detection.
- Override `health()` for a real connectivity check (wrapped in a 5 s timeout by the gateway).

## Interactive messages and correlation

When an `OutboundMessage` carries `actions` (buttons) and a `source_plugin_id`, the gateway auto-injects a `correlation_id`, persists a `PendingAction` (in-memory cache + DB write-through, surviving restarts), and stamps each `ActionButton` with the id. When the user responds, `handle_incoming()` resolves the correlation and emits the plugin's resume topic with the original `state_snapshot` attached. Pending actions expire after `LYNDRIX_GATEWAY_CORRELATION_TTL_SECONDS`; a periodic timer prunes stale entries.

## Streaming (`StreamBridge`)

LLM token streams must **not** go through the event bus. Adapters declaring `GatewayCapability.STREAMING` receive a `stream_id` in `OutboundMessage.metadata["stream_id"]` and pull tokens directly:

```python
stream_id = await messaging_gateway.stream_bridge.open()
# include stream_id in OutboundMessage.metadata, then:
async for token in llm_response:
    await messaging_gateway.stream_bridge.push(stream_id, token)
await messaging_gateway.stream_bridge.close(stream_id)
```

`StreamBridge` caps concurrent streams (`MAX_STREAMS = 50`) and closes orphaned streams older than `TTL_SECONDS` (30 min) via a periodic cleanup timer.

## Integration notes

- `dispatch()` returns `dict[provider_id, DeliveryResult]` so callers can log or react to per-adapter outcomes.
- The legacy `notification:outbound` stream is still bridged into `OutboundMessage` for code that has not migrated; prefer `messaging:outbound` or `ctx.notify(...)`.
- Stable plugin surface is exported from `core.api`: `GatewayAdapter`, `GatewayCapability`, `ProviderConfigField`, `OutboundMessage`, `InboundMessage`, `ActionButton`, `MessageSeverity`, `DeliveryResult`, `CorrelationStore`, `PendingAction`, `messaging_gateway`, `StreamBridge`.
