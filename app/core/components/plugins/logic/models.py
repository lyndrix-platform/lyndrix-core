import re
from datetime import datetime
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import List, Dict, Any, Optional

# --- NEW IMPORTS FOR DATABASE MODEL ---
from sqlalchemy import Column, Integer, String, Boolean, Text, DateTime, JSON
from core.components.database.logic.db_service import Base

_ENDPOINT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_PLACEMENT_RE = re.compile(r"^(any|singleton|pinned:[a-z][a-z0-9_-]{0,30})$")
# Panel ids are slugs: they end up in preference keys (``<plugin>/<panel>``) and DOM ids.
_PANEL_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{0,40}$")
# ``ai_instructions`` is Markdown handed to the AI assistant — keep it a note, not a book.
_AI_INSTRUCTIONS_MAX_CHARS = 20_000

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
    required_permission: Optional[str] = Field(
        default=None,
        description=(
            "Permission id gating who SEES this notification internally "
            "(e.g. 'feature:plugins.manage'). None = visible to all users."
        ),
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        if not _ENDPOINT_NAME_RE.match(value):
            raise ValueError(
                f"NotificationEndpoint name '{value}' must be snake_case ([a-z][a-z0-9_]*)"
            )
        return value


class ManifestPermission(BaseModel):
    """A plugin-declared fine-grained permission (Identity & Permissions 2.0).

    ``id`` is a LOCAL suffix — the registry namespaces it to
    ``plugin:<manifest.id>:<id>`` (e.g. ``api:infra_apply`` →
    ``plugin:lyndrix.plugin.iac_orchestrator:api:infra_apply``).
    """

    id: str = Field(..., description="Local permission suffix, e.g. 'api:infra_apply'")
    label: str = Field(..., description="Human-readable label for the permissions UI")
    description: str = Field(default="")
    icon: str = Field(default="lock")


class ManifestRole(BaseModel):
    """A plugin-declared role: a named bundle of permissions plus optional
    auto-mapping onto groups (Identity & Permissions 2.0).

    Roles are NOT database entities — they are registry metadata. ``permissions``
    entries are either local suffixes (namespaced like ManifestPermission) or
    already-qualified ids (``api:read``, ``feature:...``, ``plugin:...``)
    passed through verbatim. ``auto_map_groups`` names groups (e.g.
    ``INT_ADMIN``) whose permission list receives this bundle ONCE, guarded by
    the RoleGrantLedger so later admin revocations are never re-applied.
    """

    id: str = Field(..., description="Local role suffix, e.g. 'admin' -> role:<plugin>:admin")
    label: str = Field(..., description="Human-readable label")
    permissions: List[str] = Field(default_factory=list)
    auto_map_groups: List[str] = Field(default_factory=list)
    description: str = Field(default="")


class PluginSettingField(BaseModel):
    """Declares one user-configurable setting for a plugin.

    Mirrors the shape of EditableSettingMeta from core config so the React UI
    can render plugin settings using the same form-generation logic as system
    settings.
    """
    key: str = Field(..., description="Vault key — also used as the form field identifier")
    label: str = Field(..., description="Human-readable label shown in the UI")
    kind: str = Field(
        default="str",
        description="'str' | 'bool' | 'int' | 'select' | 'secret' "
        "('secret' values are masked in GET responses and preserved when "
        "submitted blank)",
    )
    options: List[str] = Field(default_factory=list, description="Allowed values for kind='select'")
    description: str = Field(default="", description="Help text shown below the field")
    category: str = Field(default="General", description="Groups fields into sections in the UI")
    default: Optional[str] = Field(default=None, description="Default value serialised as string")
    # kind='action' (core API >= 1.6): a button in the core's settings dialog
    # instead of a stored value. The UI calls ``<method> /api/plugins/<id><action>``
    # — a route on the plugin's own router, so the plugin's permission gates
    # apply — and shows the JSON reply's ``message``. Nothing is written to Vault.
    action: Optional[str] = Field(
        default=None,
        description="Route path relative to the plugin router, e.g. '/maintenance/prune' (kind='action' only)",
    )
    method: str = Field(default="POST", description="HTTP method for kind='action': POST | PUT | DELETE")
    confirm: bool = Field(default=False, description="Ask the operator to confirm before running (kind='action')")
    danger: bool = Field(default=False, description="Render as a destructive action (kind='action')")

    @model_validator(mode="after")
    def _validate_action(self) -> "PluginSettingField":
        if self.kind == "action":
            if not self.action or not str(self.action).startswith("/"):
                raise ValueError(
                    f"settings field '{self.key}': kind='action' needs an 'action' route path starting with '/'"
                )
            self.method = (self.method or "POST").upper()
            if self.method not in ("POST", "PUT", "DELETE"):
                raise ValueError(f"settings field '{self.key}': action method must be POST, PUT or DELETE")
        return self


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

    # --- Identity & Permissions 2.0: declared fine-grained permissions + roles ---
    custom_permissions: List[ManifestPermission] = Field(default_factory=list)
    roles: List[ManifestRole] = Field(default_factory=list)

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
    i18n_namespace: Optional[str] = Field(
        default=None,
        description="i18next-shaped translation namespace (locales/<ns>.<locale>.json) served to the React UI",
    )

    # --- Cluster placement (core API >= 1.7, docs/ha/plugin-development.md) ---
    #   any            — setup() runs on every instance (default; routes + stateless work)
    #   singleton      — setup() runs everywhere (routes!), but ctx.on_lease callbacks
    #                    are bound to ONE lease "plugin:<id>:singleton" held by one instance
    #   pinned:<role>  — setup() runs only on instances whose LYNDRIX_INSTANCE_ROLES
    #                    include <role>; other instances answer 503 "remote" for its routes
    placement: str = Field(default="any", description="any | singleton | pinned:<role>")

    # --- Panels (core API >= 1.11) ---
    # Floating, per-user pinnable windows the React shell hosts above every page.
    # The shell loads the plugin's own React bundle into the window and renders
    # ``<PluginApp panel={{id, pluginId}} />`` — a plugin that ignores the prop
    # shows its default view; a panel-aware one switches to a compact view.
    # Shape: {id (slug), label, icon (Material name), description?,
    #         default_size?: {width, height} (px), permission?}.
    panels: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="Floating shell windows [{id, label, icon, description?, default_size?, permission?}]",
    )
    # Markdown a plugin hands to the AI assistant about itself (purpose, API
    # paths, vocabulary, cautions). Collected by the assistant plugin through
    # ``core.api.module_registry``; never served to the shell.
    ai_instructions: Optional[str] = Field(
        default=None,
        description="Markdown instructions for the AI assistant about this plugin (max 20 kB)",
    )

    @field_validator("placement")
    @classmethod
    def _validate_placement(cls, value: str) -> str:
        value = (value or "any").strip().lower()
        if not _PLACEMENT_RE.match(value):
            raise ValueError(
                f"placement '{value}' is invalid — use 'any', 'singleton' or 'pinned:<role>' (role: [a-z][a-z0-9_-]*)"
            )
        return value

    @field_validator("panels")
    @classmethod
    def _validate_panels(cls, value: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Normalise and validate panel declarations (invalid → ValueError)."""
        out: List[Dict[str, Any]] = []
        seen: set[str] = set()
        for raw in value or []:
            if not isinstance(raw, dict):
                raise ValueError(f"panel declaration must be a mapping, got {type(raw).__name__}")
            pid = raw.get("id")
            if not isinstance(pid, str) or not _PANEL_ID_RE.match(pid):
                raise ValueError(f"panel id {pid!r} must be a slug ([a-z][a-z0-9_-]{{0,40}})")
            if pid in seen:
                raise ValueError(f"panel id '{pid}' is declared twice")
            seen.add(pid)
            label = raw.get("label")
            if not isinstance(label, str) or not label.strip():
                raise ValueError(f"panel '{pid}': 'label' is required")
            icon = raw.get("icon", "extension")
            if not isinstance(icon, str) or not icon.strip():
                raise ValueError(f"panel '{pid}': 'icon' must be a Material icon name")
            panel: Dict[str, Any] = {"id": pid, "label": label.strip(), "icon": icon.strip()}
            description = raw.get("description")
            if description is not None:
                if not isinstance(description, str):
                    raise ValueError(f"panel '{pid}': 'description' must be a string")
                panel["description"] = description
            size = raw.get("default_size")
            if size is not None:
                if not isinstance(size, dict):
                    raise ValueError(f"panel '{pid}': 'default_size' must be {{width, height}}")
                dims: Dict[str, int] = {}
                for dim in ("width", "height"):
                    n = size.get(dim)
                    # bool is an int subclass — reject it explicitly.
                    if not isinstance(n, int) or isinstance(n, bool) or n <= 0:
                        raise ValueError(f"panel '{pid}': default_size.{dim} must be a positive integer")
                    dims[dim] = n
                panel["default_size"] = dims
            permission = raw.get("permission")
            if permission is not None:
                if not isinstance(permission, str) or not permission.strip():
                    raise ValueError(f"panel '{pid}': 'permission' must be a permission id")
                panel["permission"] = permission.strip()
            out.append(panel)
        return out

    @field_validator("ai_instructions")
    @classmethod
    def _validate_ai_instructions(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if len(value) > _AI_INSTRUCTIONS_MAX_CHARS:
            raise ValueError(
                f"ai_instructions is {len(value)} characters — keep it under {_AI_INSTRUCTIONS_MAX_CHARS}"
            )
        return value

    @property
    def pinned_role(self) -> Optional[str]:
        return self.placement.split(":", 1)[1] if self.placement.startswith("pinned:") else None


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
    # Newest release tag the (leader's) update check found, so every instance
    # shows the same update badge (core 1.8.1).
    latest_version = Column(String(50), nullable=True)
    latest_checked_at = Column(DateTime, nullable=True)


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
    # Routing v2: LIST of provider ids (fan-out) — None = nothing configured.
    provider_bindings = Column(JSON, nullable=True)
    # Tri-state visibility override: NULL = manifest default; "" = explicitly
    # unrestricted; non-empty = required permission id.
    required_permission_override = Column(String(200), nullable=True)
    declared_defaults = Column(Text, nullable=True)
    discovered_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class PluginOperation(Base):
    """One operator-triggered plugin install/upgrade/rollback as the *cluster*
    works through it (core 1.8). The leading instance opens it with every live
    instance as participant; each instance reports its own outcome in
    ``PluginOperationInstance``; whoever closes the last participant closes the
    operation and emits exactly one ``plugin:operation_finished``."""
    __tablename__ = "plugin_operations"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plugin_id = Column(String(100), index=True, nullable=True)      # known after the first load
    repo_name = Column(String(150), index=True, nullable=True)      # folder name (safe repo name)
    action = Column(String(20), nullable=False)                     # install | upgrade | rollback
    version = Column(String(50), nullable=True)
    requested_by = Column(String(100), nullable=True)
    requested_at = Column(DateTime, nullable=False)
    status = Column(String(20), nullable=False, default="running")  # running | done | partial | failed
    finished_at = Column(DateTime, nullable=True)
    summary = Column(Text, nullable=True)                           # JSON {done, failed, pending}


class PluginOperationInstance(Base):
    __tablename__ = "plugin_operation_instances"

    operation_id = Column(Integer, primary_key=True)
    instance_id = Column(String(120), primary_key=True)
    status = Column(String(20), nullable=False, default="pending")  # pending | done | failed | reverted | skipped
    message = Column(Text, nullable=True)
    updated_at = Column(DateTime, nullable=False)
