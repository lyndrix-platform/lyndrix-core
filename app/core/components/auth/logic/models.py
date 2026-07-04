from sqlalchemy import Column, Integer, String, JSON, Boolean, DateTime
from datetime import datetime

from core.components.database.logic.db_service import Base


class User(Base):
    """Canonical User model — single source of truth for the users table."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, index=True)
    hashed_password = Column(String(255))
    full_name = Column(String(100))
    email = Column(String(100), nullable=True)
    roles = Column(JSON, default=list)
    groups = Column(JSON, default=list)              # explicit UI-assigned group memberships
    extra_permissions = Column(JSON, default=list)   # direct permission grants (no group needed)


class Group(Base):
    """
    Local group definition.

    permissions — list of permission strings, e.g.
                  ["dashboard.view", "settings.edit", "api.read"]
    ldap_mappings — list of LDAP group DNs (or AD CN strings) whose members
                    inherit this group's permissions on login.
    """
    __tablename__ = "groups"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(100), unique=True, index=True)
    description = Column(String(255), nullable=True, default="")
    permissions = Column(JSON, default=list)   # List[str]
    ldap_mappings = Column(JSON, default=list)  # List[str]  — LDAP group DNs


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
