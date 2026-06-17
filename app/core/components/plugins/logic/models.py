import re
from datetime import datetime
from pydantic import BaseModel, Field, field_validator
from typing import List, Dict, Any, Optional

# --- NEW IMPORTS FOR DATABASE MODEL ---
from sqlalchemy import Column, String, Boolean, Text, DateTime
from core.components.database.logic.db_service import Base

_ENDPOINT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# ==========================================
# 1. PYDANTIC MODELS (In-Memory Validation)
# ==========================================
class ModulePermissions(BaseModel):
    # Which Bus events is the module allowed to subscribe to?
    subscribe: List[str] = Field(default_factory=list)
    # Which events is it allowed to emit?
    emit: List[str] = Field(default_factory=list)
    # Which additional Vault paths can it access?
    vault_paths: List[str] = Field(default_factory=list)


class ModuleDependency(BaseModel):
    """Declares a dependency on another Lyndrix module."""
    id: str = Field(..., description="Module ID, e.g. 'lyndrix.plugin.git_manager'")
    version_constraint: str = Field(default="*", description="Semver constraint, e.g. '>=1.0.0,<2.0.0'")


class NotificationEndpoint(BaseModel):
    """A named notification event a plugin can route through the central router.

    Each endpoint represents a category of plugin-emitted notification (e.g.
    ``deployment_started``). At runtime the router decides — based on operator
    configuration — whether and where each one is delivered.
    """

    name: str = Field(..., description="snake_case identifier, e.g. 'deployment_succeeded'")
    description: str = Field(default="", description="Human-readable summary for the settings UI")
    default_active: bool = Field(default=True, description="Initial active state when first discovered")
    internal_toast: bool = Field(
        default=True,
        description="Raise an in-app NiceGUI toast when delivered.",
    )
    internal_persist: bool = Field(
        default=True,
        description="Append to the in-app notification history deque.",
    )
    external_default: bool = Field(
        default=False,
        description="When no explicit provider binding exists, route to the global default provider.",
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not _ENDPOINT_NAME_RE.match(value):
            raise ValueError(
                f"NotificationEndpoint name '{value}' must be snake_case ([a-z][a-z0-9_]*)"
            )
        return value


class ModuleManifest(BaseModel):
    id: str = Field(..., description="Unique ID, e.g., 'lyndrix.core.iam' or 'lyndrix.plugin.discord'")
    name: str = Field(..., description="Display name in the UI")
    version: str = Field(..., description="Semantic versioning")
    description: str = Field(default="No description provided")
    author: str = Field(default="Unknown")
    icon: str = Field(default="extension")

    # Defines whether it belongs to the system or the user
    type: str = Field(default="PLUGIN", description="'CORE' or 'PLUGIN'")
    # Only modules with a route will appear in the sidebar
    ui_route: Optional[str] = Field(default=None, description="URL path for the sidebar")

    permissions: ModulePermissions = Field(default_factory=ModulePermissions)
    settings_schema: Dict[str, Any] = Field(default_factory=dict)

    # --- NEW: Dependency & lifecycle declarations ---
    dependencies: List[ModuleDependency] = Field(default_factory=list)
    min_core_version: Optional[str] = Field(default=None, description="Minimum compatible core API version")
    auto_enable_on_install: bool = Field(default=False, description="Activate immediately after install")
    repo_url: Optional[str] = Field(default=None, description="Source repository URL for updates")

    # --- Notification routing endpoints (declared, persisted, routed by core) ---
    notification_endpoints: List[NotificationEndpoint] = Field(default_factory=list)


# ==========================================
# 2. SQLALCHEMY MODELS (Persistent Storage)
# ==========================================
class PluginState(Base):
    """
    Tracks plugin installation and activation state.
    Survives container restarts and supports version pinning.
    """
    __tablename__ = "plugin_states"

    module_id = Column(String(100), primary_key=True)
    is_active = Column(Boolean, default=False)
    installed_version = Column(String(50), nullable=True)
    desired_version = Column(String(50), nullable=True)
    repo_url = Column(String(500), nullable=True)
    auto_update = Column(Boolean, default=False)


class PluginNotificationEndpoint(Base):
    """Persistent binding state for one (plugin_id, endpoint_name) pair.

    Survives restarts. Discovery sync is run on ``db:connected`` by the
    notification router: rows are upserted from the current manifests and
    rows whose declaration disappeared are deleted.
    """
    __tablename__ = "plugin_notification_endpoints"

    plugin_id = Column(String(100), primary_key=True)
    endpoint_name = Column(String(64), primary_key=True)
    is_active = Column(Boolean, nullable=True)
    provider_binding = Column(String(200), nullable=True)
    declared_defaults = Column(Text, nullable=True)
    discovered_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)