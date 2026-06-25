import re
from datetime import datetime
from pydantic import BaseModel, Field, field_validator
from typing import List, Dict, Any, Optional

# --- NEW IMPORTS FOR DATABASE MODEL ---
from sqlalchemy import Column, Integer, String, Boolean, Text, DateTime
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


class PluginSettingField(BaseModel):
    """Declares one user-configurable setting for a plugin.

    Mirrors the shape of EditableSettingMeta from core config so the React UI
    can render plugin settings using the same form-generation logic as system
    settings.
    """
    key: str = Field(..., description="Vault key — also used as the form field identifier")
    label: str = Field(..., description="Human-readable label shown in the UI")
    kind: str = Field(default="str", description="'str' | 'bool' | 'int' | 'select'")
    options: List[str] = Field(default_factory=list, description="Allowed values for kind='select'")
    description: str = Field(default="", description="Help text shown below the field")
    category: str = Field(default="General", description="Groups fields into sections in the UI")
    default: Optional[str] = Field(default=None, description="Default value serialised as string")


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
    settings_schema: List[PluginSettingField] = Field(default_factory=list)

    # --- NEW: Dependency & lifecycle declarations ---
    dependencies: List[ModuleDependency] = Field(default_factory=list)
    min_core_version: Optional[str] = Field(default=None, description="Minimum compatible core API version")
    auto_enable_on_install: bool = Field(default=False, description="Activate immediately after install")
    repo_url: Optional[str] = Field(default=None, description="Source repository URL for updates")

    # --- Notification routing endpoints (declared, persisted, routed by core) ---
    notification_endpoints: List[NotificationEndpoint] = Field(default_factory=list)

    # --- React UI federation ---
    react_ui: bool = Field(default=False, description="Whether this plugin ships a React UI bundle")
    react_routes: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Routes exposed by the React UI bundle [{path, label, icon, sidebar_visible}]",
    )
    settings_ui_route: Optional[str] = Field(
        default=None,
        description="React route for a plugin-owned settings page; gear button navigates here instead of opening the schema modal",
    )


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


class PluginVersionHistory(Base):
    """Audit trail of plugin install / upgrade / rollback attempts and outcomes.

    One row per lifecycle event so operators can see what version was applied
    when, and whether a health-gated upgrade was auto-reverted. Pure audit — the
    authoritative current state stays in ``PluginState``.
    """
    __tablename__ = "plugin_version_history"

    id = Column(Integer, primary_key=True, index=True)
    module_id = Column(String(100), index=True, nullable=True)
    repo_name = Column(String(150), nullable=True)
    version = Column(String(50), nullable=True)
    previous_version = Column(String(50), nullable=True)
    action = Column(String(20), nullable=False)    # install | upgrade | rollback
    outcome = Column(String(20), nullable=False)   # ok | failed | reverted
    error = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class CustomPluginRepository(Base):
    """A user-added plugin repository (one Git repo == one plugin).

    These rows extend the curated ``lyndrix-plugin-collection`` marketplace with
    repositories the operator adds manually. They are merged into the same
    marketplace list and installed through the same code path as collection
    plugins. The optional per-repository API token is **never** stored here — it
    lives in Vault (path ``core/plugin_repos``, key ``repo_{id}_token``); this
    row only records whether one exists via ``has_token``.
    """
    __tablename__ = "custom_plugin_repositories"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    repo_url = Column(String(500), unique=True, nullable=False)
    provider = Column(String(20), default="github")  # "github" | "gitlab"
    description = Column(String(500), nullable=True)
    enabled = Column(Boolean, default=True)
    has_token = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)


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