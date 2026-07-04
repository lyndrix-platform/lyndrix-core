"""
Group management service.

Groups are local role/permission containers that can optionally receive members
automatically from LDAP group DN mappings during login.

Usage
-----
    from core.components.auth.logic.group_service import group_service

    # CRUD
    grp = group_service.create("ops", "Operations team", ["dashboard.view"])
    group_service.update(grp.id, permissions=["dashboard.view", "api.read"])
    group_service.delete(grp.id)

    # LDAP mapping
    group_service.add_ldap_mapping(grp.id, "cn=ops,ou=groups,dc=example,dc=com")

    # Permission check (used in API / views)
    has = group_service.user_has_permission(user_roles, "dashboard.view")
"""
from typing import List, Optional

from core.logger import get_logger
from core.components.database.logic.db_service import db_instance
from .models import Group, User

log = get_logger("Core:GroupService")


class GroupService:
    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get_all(self) -> List[Group]:
        with db_instance.SessionLocal() as s:
            return s.query(Group).order_by(Group.name).all()

    def get_by_id(self, group_id: int) -> Optional[Group]:
        with db_instance.SessionLocal() as s:
            return s.query(Group).filter(Group.id == group_id).first()

    def get_by_name(self, name: str) -> Optional[Group]:
        with db_instance.SessionLocal() as s:
            return s.query(Group).filter(Group.name == name).first()

    def get_for_ldap_group(self, ldap_dn: str) -> List[Group]:
        """Return all local groups that have `ldap_dn` in their ldap_mappings."""
        with db_instance.SessionLocal() as s:
            # JSON_CONTAINS would be cleaner but we keep it portable
            all_groups = s.query(Group).all()
            return [g for g in all_groups if ldap_dn in (g.ldap_mappings or [])]

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def create(
        self,
        name: str,
        description: str = "",
        permissions: Optional[List[str]] = None,
        ldap_mappings: Optional[List[str]] = None,
    ) -> Group:
        with db_instance.SessionLocal() as s:
            grp = Group(
                name=name.strip(),
                description=description,
                permissions=list(permissions or []),
                ldap_mappings=list(ldap_mappings or []),
            )
            s.add(grp)
            s.commit()
            s.refresh(grp)
            log.info(f"GROUP: Created '{grp.name}' (id={grp.id})")
            return grp

    def update(
        self,
        group_id: int,
        name: Optional[str] = None,
        description: Optional[str] = None,
        permissions: Optional[List[str]] = None,
        ldap_mappings: Optional[List[str]] = None,
    ) -> Optional[Group]:
        with db_instance.SessionLocal() as s:
            grp = s.query(Group).filter(Group.id == group_id).first()
            if not grp:
                log.warning(f"GROUP: update() — id={group_id} not found.")
                return None
            if name is not None:
                grp.name = name.strip()
            if description is not None:
                grp.description = description
            if permissions is not None:
                grp.permissions = list(permissions)
            if ldap_mappings is not None:
                grp.ldap_mappings = list(ldap_mappings)
            s.commit()
            s.refresh(grp)
            log.info(f"GROUP: Updated '{grp.name}' (id={grp.id})")
            # Group permissions feed every member's effective set — drop the
            # short-TTL authz cache so edits apply on the next request.
            from . import access_service
            access_service.invalidate_cache()
            return grp

    def delete(self, group_id: int) -> bool:
        with db_instance.SessionLocal() as s:
            grp = s.query(Group).filter(Group.id == group_id).first()
            if not grp:
                return False
            name = grp.name
            s.delete(grp)
            s.commit()
            log.info(f"GROUP: Deleted '{name}' (id={group_id})")
            return True

    def add_ldap_mapping(self, group_id: int, ldap_dn: str) -> bool:
        with db_instance.SessionLocal() as s:
            grp = s.query(Group).filter(Group.id == group_id).first()
            if not grp:
                return False
            mappings: list = list(grp.ldap_mappings or [])
            if ldap_dn not in mappings:
                mappings.append(ldap_dn)
                grp.ldap_mappings = mappings
                s.commit()
                log.info(f"GROUP: LDAP mapping added to '{grp.name}': {ldap_dn}")
            return True

    def remove_ldap_mapping(self, group_id: int, ldap_dn: str) -> bool:
        with db_instance.SessionLocal() as s:
            grp = s.query(Group).filter(Group.id == group_id).first()
            if not grp:
                return False
            mappings: list = list(grp.ldap_mappings or [])
            if ldap_dn in mappings:
                mappings.remove(ldap_dn)
                grp.ldap_mappings = mappings
                s.commit()
                log.info(f"GROUP: LDAP mapping removed from '{grp.name}': {ldap_dn}")
            return True

    # ------------------------------------------------------------------
    # Permission helpers (use in views / API guards)
    # ------------------------------------------------------------------

    def get_permissions_for_roles(self, roles: List[str]) -> List[str]:
        """
        Collect all permissions granted by the given GROUP names.

        Identity 2.0 note: despite the legacy parameter name, callers must pass
        group names (``User.groups``) — ``User.roles`` are system flags and are
        never resolved here. Prefer ``access_service`` for authorization.
        """
        perms: set = set()
        with db_instance.SessionLocal() as s:
            groups = (
                s.query(Group)
                .filter(Group.name.in_(roles))
                .all()
            )
            for grp in groups:
                perms.update(grp.permissions or [])
        return sorted(perms)

    def user_has_permission(
        self,
        roles: List[str],
        permission: str,
        extra_permissions: Optional[List[str]] = None,
    ) -> bool:
        """
        Quick guard — returns True if any group matching a role grants `permission`,
        or if the permission is in the user's direct extra_permissions list.

            if group_service.user_has_permission(user_roles, "settings.edit", user_extra):
                ...
        """
        if extra_permissions and permission in extra_permissions:
            return True
        return permission in self.get_permissions_for_roles(roles)

    # ------------------------------------------------------------------
    # User ↔ Group assignment
    # ------------------------------------------------------------------

    def get_group_members(self, group_id: int) -> List[User]:
        """Return all users explicitly assigned to this group."""
        grp = self.get_by_id(group_id)
        if not grp:
            return []
        with db_instance.SessionLocal() as s:
            all_users = s.query(User).order_by(User.username).all()
            return [u for u in all_users if grp.name in (u.groups or [])]

    def get_user_groups(self, username: str) -> List[Group]:
        """Return all groups a user is explicitly a member of."""
        with db_instance.SessionLocal() as s:
            user = s.query(User).filter(User.username == username).first()
            if not user or not user.groups:
                return []
            return (
                s.query(Group)
                .filter(Group.name.in_(list(user.groups)))
                .order_by(Group.name)
                .all()
            )

    def add_user_to_group(self, group_id: int, username: str) -> bool:
        grp = self.get_by_id(group_id)
        if not grp:
            return False
        with db_instance.SessionLocal() as s:
            user = s.query(User).filter(User.username == username).first()
            if not user:
                return False
            groups = list(user.groups or [])
            if grp.name not in groups:
                groups.append(grp.name)
                user.groups = sorted(groups)
                s.commit()
                log.info(f"GROUP: Added '{username}' to group '{grp.name}'.")
                from . import access_service
                access_service.invalidate_cache(username)
        return True

    def remove_user_from_group(self, group_id: int, username: str) -> bool:
        grp = self.get_by_id(group_id)
        if not grp:
            return False
        with db_instance.SessionLocal() as s:
            user = s.query(User).filter(User.username == username).first()
            if not user:
                return False
            groups = list(user.groups or [])
            if grp.name in groups:
                groups.remove(grp.name)
                user.groups = groups
                s.commit()
                log.info(f"GROUP: Removed '{username}' from group '{grp.name}'.")
                from . import access_service
                access_service.invalidate_cache(username)
        return True

    def resolve_ldap_groups(self, ldap_group_dns: List[str]) -> List[str]:
        """
        Given a list of raw LDAP group DN strings (from the directory), return
        the names of all matching local groups.  Used during LDAP login to
        augment the user's role list.
        """
        local_group_names: set = set()
        with db_instance.SessionLocal() as s:
            all_groups = s.query(Group).all()
            for grp in all_groups:
                for dn in (grp.ldap_mappings or []):
                    if dn in ldap_group_dns:
                        local_group_names.add(grp.name)
                        break
        return sorted(local_group_names)


# Singleton
group_service = GroupService()
