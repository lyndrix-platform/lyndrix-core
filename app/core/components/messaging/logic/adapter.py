"""
GatewayAdapter ABC and GatewayCapability flags.

External plugins subclass ``GatewayAdapter`` and register themselves via
``ctx.register_gateway_adapter(adapter)`` in their ``setup()`` hook.
The Gateway calls ``send()`` for outbound messages and ``handle_incoming()``
for provider-sourced interactions (button clicks, replies, etc.).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Flag, auto
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from core.components.messaging.model.schemas import InboundMessage, OutboundMessage


@dataclass
class ProviderConfigField:
    """Describes one user-configurable setting for a GatewayAdapter.

    Adapters that override ``get_config_fields()`` return a list of these so
    the notification router UI can render inline edit forms with proper env-lock
    detection without needing to know the adapter's internal config scheme.
    """

    key: str
    label: str
    env_var: str
    current_value: str | None = None
    is_env_locked: bool = False
    sensitive: bool = False
    placeholder: str = ""


class GatewayCapability(Flag):
    """Feature flags that describe what a GatewayAdapter can do.

    Declare capabilities as a class variable on your adapter subclass so the
    Gateway and settings UI can dynamically show/hide relevant options.
    """
    TEXT             = auto()  # Plain text or basic rich text
    RICH_MEDIA       = auto()  # Embeds, thumbnails, structured content
    FILE_ATTACHMENTS = auto()  # Binary file uploads
    INTERACTIVE      = auto()  # Action buttons backed by Correlation IDs
    THREADS          = auto()  # Threaded/reply-chain messages
    REACTIONS        = auto()  # Emoji reactions on messages
    TYPING_INDICATOR = auto()  # "Bot is typing…" signal
    EDIT_MESSAGE     = auto()  # Edit a previously sent message
    DELETE_MESSAGE   = auto()  # Delete a previously sent message
    # STREAMING is reserved: LLM token streams MUST use StreamBridge directly,
    # never the EventBus.  Declare this flag only if your adapter exposes a
    # StreamBridge endpoint.
    STREAMING        = auto()


class GatewayAdapter(ABC):
    """Base class for all Messaging Gateway provider adapters.

    Subclass this in your plugin, declare the three ClassVars, and implement
    at minimum ``send()``.  Override ``handle_incoming()`` if your adapter
    supports ``GatewayCapability.INTERACTIVE``.

    Example::

        class SlackAdapter(GatewayAdapter):
            provider_id  = "slack"
            display_name = "Slack"
            capabilities = GatewayCapability.TEXT | GatewayCapability.INTERACTIVE

            async def send(self, message: OutboundMessage) -> str | None:
                ...
    """

    provider_id:  ClassVar[str]               # Stable unique identifier, e.g. "discord"
    display_name: ClassVar[str]               # Human-readable name for the settings UI
    capabilities: ClassVar[GatewayCapability] # Feature flag bitmask
    send_timeout: ClassVar[float] = 30.0     # Per-adapter send timeout (seconds); override to tune

    @abstractmethod
    async def send(self, message: "OutboundMessage") -> str | None:
        """Deliver *message* to the external provider.

        Returns a provider-specific message ID string when available (useful
        for later ``EDIT_MESSAGE`` or ``DELETE_MESSAGE`` calls), or ``None``.
        Must never raise — log and return ``None`` on failure.
        """

    async def handle_incoming(self, raw: dict) -> "InboundMessage | None":
        """Parse a raw incoming webhook payload from this provider.

        Override this method if the adapter declares ``GatewayCapability.INTERACTIVE``.
        Return ``None`` if the payload is not relevant (e.g. a ping/health-check).
        The Gateway calls this from the provider's FastAPI interaction endpoint.
        """
        return None

    def supports(self, cap: GatewayCapability) -> bool:
        """True when this adapter declares the given capability flag."""
        return bool(self.capabilities & cap)

    async def health(self) -> bool:
        """Return True if this adapter is operational.

        The default implementation always returns True.  Override to perform a
        real connectivity check (e.g. resolve the webhook URL, call a status
        API).  The gateway wraps this in a 5-second timeout so a slow check
        cannot block the health endpoint.
        """
        return True

    def get_config_fields(self) -> list[ProviderConfigField]:
        """Return the list of user-configurable fields for this adapter.

        Override this in your adapter to enable inline config in the
        Notifications settings UI. Return an empty list (the default) to
        show only the read-only env-var status rows.
        """
        return []

    def save_config(self, key: str, value: str) -> bool:
        """Persist a config value for this adapter (e.g. to Vault).

        Called by the Notifications settings UI when the operator edits a
        field that is not locked by an env var. Return ``True`` on success.
        Override alongside ``get_config_fields()``.
        """
        return False
