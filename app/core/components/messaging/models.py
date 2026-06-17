"""
Strict Pydantic v2 message payload schemas for the Lyndrix Messaging Gateway.

All cross-provider communication passes through these types so that adapters
never need to agree on ad-hoc dict shapes — only on these schemas.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, HttpUrl


class MessageSeverity(str, Enum):
    INFO    = "info"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR   = "error"


class ActionButton(BaseModel):
    """An interactive button attached to an OutboundMessage.

    The ``correlation_id`` field is injected by the MessagingGateway just
    before dispatch — callers must NOT set it themselves.
    """
    action_id:    str
    label:        str
    style:        Literal["primary", "danger", "secondary"] = "primary"
    confirm_text: str | None = None
    # Set by Gateway, not by the emitting plugin:
    correlation_id: UUID | None = Field(default=None)


class OutboundMessage(BaseModel):
    """A message a plugin wishes to send via one or all registered adapters.

    Capability-gated fields (``image_url``, ``file_paths``, ``thread_id``)
    are silently ignored by adapters that don't declare the matching
    ``GatewayCapability`` flag.
    """
    message_id:      UUID            = Field(default_factory=uuid4)
    title:           str
    body:            str
    severity:        MessageSeverity = MessageSeverity.INFO
    source_plugin_id: str | None     = None
    # Routing: None = broadcast to all registered adapters
    target_provider: str | None      = None
    target_channel:  str | None      = None
    # Correlation: set this if you want to resume on user response
    correlation_id:  UUID | None     = None
    actions:         list[ActionButton] = Field(default_factory=list)
    # --- Capability-gated fields ---
    image_url:       HttpUrl | None  = None
    file_paths:      list[str]       = Field(default_factory=list)
    thread_id:       str | None      = None
    # Arbitrary adapter-specific hints (e.g. {"stream_id": "xyz"} for STREAMING)
    metadata:        dict[str, Any]  = Field(default_factory=dict)


class DeliveryResult(BaseModel):
    """Outcome of a single adapter dispatch attempt returned by ``MessagingGateway.dispatch()``."""

    provider_id:       str
    success:           bool
    message_id:        str | None = None
    error:             str | None = None
    queued_for_retry:  bool = False
    attempts:          int  = 1


class InboundMessage(BaseModel):
    """A user response arriving from an external provider.

    The Gateway resolves ``correlation_id`` against the CorrelationStore and
    emits the originating plugin's ``resume_topic`` with this payload attached.
    """
    provider_id:    str
    event_type:     str
    correlation_id: UUID | None
    action_id:      str | None
    user_id:        str | None
    text_response:  str | None      = None
    metadata:       dict[str, Any]  = Field(default_factory=dict)
    received_at:    datetime        = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
