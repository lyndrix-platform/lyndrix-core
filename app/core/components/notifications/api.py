from __future__ import annotations

from typing import Any, Dict, List, Tuple

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Request

from core.api.security import ApiIdentity, require_api_auth
from core.logger import get_logger
from .notification_service import notification_service

log = get_logger("Core:NotificationAPI")


def _severity_for_pipeline(status: str) -> Tuple[str, bool]:
    normalized = (status or "").lower()

    if normalized in {"success", "passed"}:
        return "positive", True
    if normalized in {"failed", "error"}:
        return "negative", True
    if normalized in {"canceled", "cancelled", "skipped", "manual"}:
        return "warning", True
    if normalized in {"running", "pending", "created", "preparing", "waiting_for_resource"}:
        return "ongoing", False

    return "info", True


def _build_pipeline_notification(payload: Dict[str, Any]) -> Dict[str, Any]:
    attrs = payload.get("object_attributes") or {}
    project = payload.get("project") or {}
    user = payload.get("user") or {}

    project_name = project.get("path_with_namespace") or project.get("name") or "unknown-project"
    pipeline_id = attrs.get("id") or attrs.get("iid") or "unknown"
    ref = attrs.get("ref") or "unknown"
    status = attrs.get("status") or "unknown"
    source = attrs.get("source") or "unknown"
    url = attrs.get("url") or ""
    username = user.get("username") or user.get("name") or "system"

    notif_type, toast = _severity_for_pipeline(status)

    title = f"GitLab Pipeline #{pipeline_id}"
    message = f"{project_name} | {status.upper()} | ref={ref} | source={source} | by={username}"
    if url:
        message = f"{message} | {url}"

        return {
        "id": f"gitlab:pipeline:{project_name}:{pipeline_id}",
        "title": title,
        "message": message,
        "type": notif_type,
        "toast": toast,
            "emit_outbound": notif_type in {"positive", "negative", "warning"},
    }


def register_notification_fastapi_routes(fastapi_app: FastAPI) -> None:
    @fastapi_app.get("/api/notifications/webhook/gitlab/health")
    async def gitlab_webhook_health():
        ctx = notification_service.ctx
        if not ctx:
            raise HTTPException(status_code=503, detail="Notification service not initialized")

        token_configured = bool(ctx.get_secret("gitlab_webhook_token"))
        return {
            "status": "ok",
            "service": "gitlab_notification_ingress",
            "token_configured": token_configured,
            "security_mode": "disabled_temporarily",
            "webhook_endpoint": "/api/notifications/webhook/gitlab",
            "test_base_url": "http://10.1.10.31:8081",
            "test_webhook_url": "http://10.1.10.31:8081/api/notifications/webhook/gitlab",
        }

    @fastapi_app.post("/api/notifications/webhook/gitlab")
    async def receive_gitlab_notification(
        request: Request,
        x_gitlab_event: str = Header(default="", alias="X-Gitlab-Event"),
    ):
        # TODO(security): protect this generic compatibility endpoint with auth/token validation.
        ctx = notification_service.ctx
        if not ctx:
            raise HTTPException(status_code=503, detail="Notification service not initialized")

        try:
            payload = await request.json()
        except Exception:
            raise HTTPException(status_code=400, detail="Malformed JSON payload")

        # Generic compatibility mode: allow direct notification payloads.
        if "title" in payload and "message" in payload:
            await notification_service.handle_system_notify({
                "id": payload.get("id"),
                "title": payload.get("title", "Notification"),
                "message": payload.get("message", ""),
                "type": payload.get("type", "info"),
                "toast": payload.get("toast", True),
                "user_id": payload.get("user_id"),
            })
            return {
                "status": "accepted",
                "mode": "generic",
            }

        object_kind = (payload.get("object_kind") or "").lower()
        header_event = (x_gitlab_event or "").lower()

        is_pipeline_event = object_kind == "pipeline" or "pipeline" in header_event
        if not is_pipeline_event:
            return {
                "status": "ignored",
                "reason": "Only pipeline events are accepted",
                "object_kind": object_kind or "unknown",
            }

        notif = _build_pipeline_notification(payload)
        await notification_service.handle_system_notify(notif)

        log.info(
            "WEBHOOK: Accepted pipeline webhook for %s",
            notif["id"],
        )
        return {
            "status": "accepted",
            "notification_id": notif["id"],
            "type": notif["type"],
        }


# ──────────────────────────────────────────────────────────────────────────────
# Authenticated notification feed for the React frontend (notification bell).
#
# Scoped to the current identity's username: a user sees their own targeted
# notifications (user_id == username) plus broadcasts (user_id is None).
# ──────────────────────────────────────────────────────────────────────────────

notifications_router = APIRouter(prefix="/api/notifications", tags=["Notifications"])


def _visible_for(username: str) -> List[Dict[str, Any]]:
    """Notifications visible to ``username``: own + broadcast (user_id is None).

    ``history`` is appendleft-ordered (newest first), so iteration order already
    yields newest-first.
    """
    return [
        n for n in list(notification_service.history)
        if n.get("user_id") in (username, None)
    ]


def _require_visible(notif_id: str, username: str) -> Dict[str, Any]:
    """Resolve a notification by id and assert the caller may act on it.

    A user may only mutate their own targeted notifications (``user_id == username``)
    or broadcasts (``user_id is None``). Anything else 404s — never reveal that an
    id belonging to another user exists. Prevents write-side IDOR on read/dismiss.
    """
    match = next(
        (n for n in list(notification_service.history) if n.get("id") == notif_id),
        None,
    )
    if match is None or match.get("user_id") not in (username, None):
        raise HTTPException(status_code=404, detail="Notification not found")
    return match


def _public_view(n: Dict[str, Any]) -> Dict[str, Any]:
    """Strip the internal ``user_id`` from a notification for the API response."""
    return {
        "id": n["id"],
        "title": n.get("title", ""),
        "message": n.get("message", ""),
        "type": n.get("type", "info"),
        "timestamp": n.get("timestamp"),
        "read": bool(n.get("read", False)),
    }


@notifications_router.get("", summary="List the current user's notifications")
async def list_notifications(identity: ApiIdentity = Depends(require_api_auth)):
    visible = _visible_for(identity.username)
    return {
        "notifications": [_public_view(n) for n in visible],
        "unread": sum(1 for n in visible if not n.get("read", False)),
    }


@notifications_router.post("/{notif_id}/read", summary="Mark a notification as read")
async def read_notification(
    notif_id: str,
    identity: ApiIdentity = Depends(require_api_auth),
):
    _require_visible(notif_id, identity.username)
    notification_service.mark_as_read(notif_id)
    return {"status": "ok"}


@notifications_router.post("/read-all", summary="Mark all visible notifications as read")
async def read_all_notifications(identity: ApiIdentity = Depends(require_api_auth)):
    for n in _visible_for(identity.username):
        notification_service.mark_as_read(n["id"])
    return {"status": "ok"}


@notifications_router.delete("/{notif_id}", summary="Dismiss a notification")
async def dismiss_notification(
    notif_id: str,
    identity: ApiIdentity = Depends(require_api_auth),
):
    _require_visible(notif_id, identity.username)
    notification_service.remove_notification(notif_id)
    return {"status": "ok"}


@notifications_router.delete("", summary="Clear all visible notifications")
async def clear_notifications(identity: ApiIdentity = Depends(require_api_auth)):
    # NOTE: "visible" includes broadcasts (user_id is None), so clearing all also
    # removes shared broadcast notifications from the store — this matches the
    # user-facing "Alle löschen" expectation but is a documented side effect.
    for nid in [n["id"] for n in _visible_for(identity.username)]:
        notification_service.remove_notification(nid)
    return {"status": "ok"}
