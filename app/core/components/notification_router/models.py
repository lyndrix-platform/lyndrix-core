"""Pydantic schemas for the notification router.

The envelope is the strict contract every emitted notification must satisfy.
Plugins build it through ``ctx.notify(...)``; the router validates that the
declared ``(source_plugin_id, endpoint_name)`` exists before dispatching.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from core.components.messaging.model.schemas import MessageSeverity


class NotificationEnvelope(BaseModel):
    """Wire format for any plugin-emitted notification."""

    envelope_version: Literal["1"] = "1"
    source_plugin_id: str
    endpoint_name: str
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    payload: dict[str, Any] = Field(default_factory=dict)

    # Optional metadata for routing and UI integration
    notification_id: str | None = Field(
        default=None,
        description="Stable id for sticky/updatable notifications and clear semantics.",
    )
    correlation_id: UUID | None = None
    title: str | None = None
    body: str | None = None
    severity: MessageSeverity = MessageSeverity.INFO
    clear: bool = Field(
        default=False,
        description="When True together with notification_id, the router removes the notification.",
    )


class ResolvedState(BaseModel):
    """Effective state of an endpoint after applying env > DB > default precedence."""

    active: bool
    # Routing v2: list of external provider ids (fan-out). [] = internal only.
    providers: list[str] = Field(default_factory=list)
    active_source: Literal["env", "db", "default"] = "default"
    provider_source: Literal["env", "db", "default", "global_default", "none"] = "none"
    # Visibility gate for the INTERNAL feed/toast/SSE (None = everyone).
    required_permission: str | None = None
    required_permission_source: Literal["env", "db", "default", "none"] = "none"
