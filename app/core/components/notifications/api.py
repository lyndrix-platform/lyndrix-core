from __future__ import annotations

from typing import Any, Dict, Tuple

from fastapi import FastAPI, Header, HTTPException, Request

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
