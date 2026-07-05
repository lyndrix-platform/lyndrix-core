import uuid as _uuid
from sqlalchemy import Column, Integer, String, JSON, Boolean, DateTime, UniqueConstraint
from datetime import datetime

from core.components.database.logic.db_service import Base


class User(Base):
    """Canonical User model — single source of truth for the users table.

    Identity & Permissions 2.0 (1.0 release):
    - ``id`` is a stable UUID string — the global identifier across providers.
    - ``hashed_password`` is nullable: SSO-only users have no local password.
    - ``roles`` carries SYSTEM FLAGS ONLY (``superadmin``, ``bot``) — it is no
      longer matched against ``Group.name``. Effective permissions come from
      ``groups`` (union of Group.permissions) plus ``extra_permissions``,
      resolved by ``access_service``.
    """
    __tablename__ = "users"

    id = Column(String(36), primary_key=True, default=lambda: str(_uuid.uuid4()))
    username = Column(String(50), unique=True, index=True, nullable=False)
    hashed_password = Column(String(255), nullable=True)
    full_name = Column(String(100))
    email = Column(String(100), nullable=True, index=True)
    roles = Column(JSON, default=list)               # system flags only ("superadmin", "bot")
    groups = Column(JSON, default=list)              # explicit group memberships (sole source)
    extra_permissions = Column(JSON, default=list)   # direct permission grants (no group needed)


class UserIdentity(Base):
    """One row per (provider, external account) linked to a local User.

    ``provider_user_id`` is the provider's *stable* identifier — AD ``objectSid``
    (preferred) or DN for LDAP, ``sub`` for OIDC, the username for local.
    ``matched_by`` records how the link was established (audit): ``email`` |
    ``sid`` | ``sub`` | ``username`` | ``new`` | ``manual``.
    """
    __tablename__ = "user_identities"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String(36), index=True, nullable=False)   # -> User.id (UUID)
    provider = Column(String(20), nullable=False)               # "local" | "ldap" | "oidc"
    provider_user_id = Column(String(255), nullable=False)
    matched_by = Column(String(20), nullable=False, default="new")
    email_at_link = Column(String(100), nullable=True)          # snapshot for audit
    created_at = Column(DateTime, default=datetime.utcnow)
    last_login_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint("provider", "provider_user_id", name="uq_identity_provider_account"),
    )


class RoleGrantLedger(Base):
    """Idempotency marker for manifest-role → group auto-mapping.

    A (role_id, group_name) pair is applied AT MOST ONCE: when a plugin's
    ``ManifestRole.auto_map_groups`` first attaches its permissions to a
    default group, a ledger row is written. An admin who later revokes one of
    those permissions from the group will NOT have it silently re-added on the
    next boot / plugin re-activation.
    """
    __tablename__ = "role_grant_ledger"

    role_id = Column(String(150), primary_key=True)     # "role:<plugin_id>:<suffix>"
    group_name = Column(String(100), primary_key=True)  # e.g. "INT_ADMIN"
    applied_at = Column(DateTime, default=datetime.utcnow)


class Group(Base):
    """
    Local group definition.

    permissions — list of permission strings, e.g.
                  ["dashboard.view", "settings.edit", "api.read"]
    ldap_mappings — list of LDAP group DNs (or AD CN strings) whose members
                    inherit this group's permissions on login.
    roles — list of registered role ids (``role_registry.RoleDef.id``)
            assigned to this group. Stored and expanded at resolution time
            (``access_service.get_effective_permissions``) rather than melted
            into ``permissions`` once, so a role's permission set stays live
            when a plugin upgrade changes what the role grants. Distinct from
            ``User.roles``, which carries system flags only.
    """
    __tablename__ = "groups"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, index=True)
    description = Column(String(255), nullable=True, default="")
    permissions = Column(JSON, default=list)   # List[str]
    ldap_mappings = Column(JSON, default=list)  # List[str]  — LDAP group DNs
    roles = Column(JSON, default=list)         # List[str]  — assigned role ids


class UserPreference(Base):
    """
    Per-user, namespaced preference document (Theming v2 "My account" scope).

    A single JSON blob per user holding cross-device preferences — theme
    selection (react/nicegui), token overrides, language, layout/visibility.
    Kept deliberately generic: the server treats ``data`` as an opaque
    namespaced dict and deep-merges partial patches into it. One row per user.
    """
    __tablename__ = "user_preferences"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True)  # owner username
    data = Column(JSON, default=dict)                        # namespaced prefs dict
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class UserApiKey(Base):
    """
    A per-user API key for machine-to-machine HTTP access.

    The raw key is shown to the user exactly once at creation time; only its
    SHA-256 hash is stored.  ``scopes`` optionally restricts the key to a subset
    of the owner's permissions (e.g. ["api:read"]); an empty list means the key
    inherits the owner's full effective permissions.
    """
    __tablename__ = "user_api_keys"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), index=True)          # owner username
    label = Column(String(100), default="API Key")     # human-friendly name
    prefix = Column(String(16), index=True)            # visible key prefix for display
    hashed_key = Column(String(64), unique=True, index=True)  # sha256 hex digest
    scopes = Column(JSON, default=list)                # subset of api:* perms; [] => inherit
    created_at = Column(DateTime, default=datetime.utcnow)
    last_used_at = Column(DateTime, nullable=True)
    revoked = Column(Boolean, default=False)
