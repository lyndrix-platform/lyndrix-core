# app/core/services.py
from core.components.vault.logic.vault_service import vault_instance
from core.components.database.logic.db_service import db_instance
from core.components.auth.logic.auth_service import auth_service
from core.components.boot.logic.boot_service import boot_service
from core.components.vault.logic.auto_unseal import auto_unseal_manager
from core.components.plugins.logic.plugin_service import plugin_service
from core.components.system.logic.monitor_service import monitor_service
from core.components.notifications.notification_service import notification_service
from core.components.git.logic.git_service import git_service

__all__ = [
    "vault_instance", "db_instance", "auth_service",
    "boot_service", "auto_unseal_manager", "plugin_service",
    "monitor_service", "notification_service", "git_service",
]