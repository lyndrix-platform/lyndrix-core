from sqlalchemy import Column, Integer, String, JSON
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
