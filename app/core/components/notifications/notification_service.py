import time
import os
import json
from collections import deque
from nicegui import ui
from nicegui.client import Client
from config import settings
from core.bus import bus

class NotificationService:
    def __init__(self):
        self.history = deque(maxlen=500)
        self.ctx = None
        self.storage_file = os.path.join(settings.STORAGE_DIR, "notifications.json")
        self._load()

    def _load(self):
        if os.path.exists(self.storage_file):
            try:
                with open(self.storage_file, 'r') as f:
                    items = json.load(f)
                    self.history = deque(items, maxlen=500)
            except Exception:
                pass

    def _save(self):
        try:
            os.makedirs(os.path.dirname(self.storage_file), exist_ok=True)
            tmp_file = self.storage_file + ".tmp"
            with open(tmp_file, 'w') as f:
                json.dump(list(self.history), f)
            os.replace(tmp_file, self.storage_file)
        except Exception:
            pass

    def set_context(self, ctx):
        self.ctx = ctx

    async def handle_system_notify(self, payload: dict):
        self._process_notification(payload, broadcast=True)

    async def handle_user_notify(self, payload: dict):
        self._process_notification(payload, broadcast=False)

    def _process_notification(self, payload: dict, broadcast: bool):
        notif_id = payload.get("id") or str(time.time())
        action = payload.get("action", "upsert")
        persist = payload.get("persist", True)

        if action == "clear":
            self.remove_notification(notif_id)
            return

        message = payload.get("message", "System Notification")
        type_ = payload.get("type", "info") 
        title = payload.get("title", "System")
        user_id = payload.get("user_id") if not broadcast else None
        
        existing = next((n for n in self.history if n['id'] == notif_id), None)
        notif = {
            "id": notif_id, "timestamp": time.time(), "title": title,
            "message": message, "type": type_, "user_id": user_id,
            "read": False
        }

        if persist:
            if existing:
                # Update an existing notification in place and bump it to the top.
                existing.update({
                    'message': message, 'type': type_, 'title': title,
                    'timestamp': time.time(), 'read': False
                })
                self.history.remove(existing)
                self.history.appendleft(existing)
                notif = existing
                if self.ctx:
                    self.ctx.log.info(f"[UPDATE: {title}] {message}")
            else:
                self.history.appendleft(notif)
                if self.ctx:
                    self.ctx.log.info(f"[{title}] {message}")
        elif self.ctx:
            self.ctx.log.info(f"[TRANSIENT: {title}] {message}")

        # Only show a floating toast if explicitly requested and NOT an ongoing background task
        if broadcast and payload.get("toast", True) and type_ != "ongoing":
            self.broadcast_toast(message, type_)

        if self.ctx and payload.get("emit_outbound", True):
            self.ctx.emit("notification:outbound", notif)

        if persist:
            self._save()
            # Notify connected React frontends (notification bell) over SSE.
            # Additive — does not affect the existing outbound/toast behaviour.
            bus.emit("notification:new", {
                "id": notif["id"],
                "title": notif["title"],
                "message": notif["message"],
                "type": notif["type"],
                "timestamp": notif["timestamp"],
                "user_id": notif["user_id"],
            })

    def broadcast_toast(self, message: str, type_: str):
        toast_type = type_ if type_ in ['positive', 'negative', 'warning', 'info'] else 'info'
        for client in list(Client.instances.values()):
            try:
                if client.has_socket_connection:
                    with client:
                        ui.notify(message, type=toast_type, position="top-right", multi_line=True, timeout=3.0)
            except Exception:
                pass

    def remove_notification(self, notif_id: str):
        self.history = deque([n for n in self.history if n['id'] != notif_id], maxlen=500)
        self._save()

    def get_unread_for_user(self, user_id: str):
        return [n for n in self.history if (n['user_id'] == user_id or n['user_id'] is None) and not n['read']]

    def mark_as_read(self, notif_id: str):
        for n in self.history:
            if n['id'] == notif_id:
                n['read'] = True
        self._save()

notification_service = NotificationService()
