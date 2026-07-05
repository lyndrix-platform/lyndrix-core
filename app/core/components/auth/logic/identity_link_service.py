"""External-identity linking (Identity & Permissions 2.0).

Every successful authentication — local, LDAP or OIDC — resolves to exactly
one local ``User`` row plus a ``UserIdentity`` link keyed by the provider's
*stable* account id (AD objectSid / DN, OIDC ``sub``, local username). The
same human logging in through two providers lands on ONE local profile with
two identity rows.

Resolution order for an unseen (provider, provider_user_id):
  1. trusted-email auto-match — the provider vouches for the email
     (``AuthResult.email_verified``) and it matches exactly ONE local user
     that has no identity for this provider yet;
  2. username match for the LOCAL provider only (the row is the account);
  3. otherwise a brand-new user (fresh UUID, default groups from
     ``LYNDRIX_SSO_DEFAULT_GROUPS``, no local password).

Ambiguity (an email matching several users) NEVER silently merges — a new
account is created and a warning logged for admin review. Username collisions
with an unlinked local account never hijack it: the new account gets a
``<name>_<provider>`` (or numbered) username instead.

All functions are synchronous DB work — callers on the event loop must wrap
them in ``asyncio.to_thread`` (the provider registry does).
"""
from __future__ import annotations

import os
from typing import Optional

from core.logger import get_logger
from core.components.database.logic.db_service import db_instance
from .models import Group, User, UserIdentity
from .providers.base import AuthResult

log = get_logger("Core:IdentityLink")

_DEFAULT_SSO_GROUPS = [
    g.strip()
    for g in os.getenv("LYNDRIX_SSO_DEFAULT_GROUPS", "INT_VIEWER").split(",")
    if g.strip()
]


def _unique_username(session, wanted: str, provider: str) -> str:
    """Return ``wanted`` if free, else a provider-suffixed/numbered variant."""
    base = (wanted or f"{provider}_user").strip()[:40] or f"{provider}_user"
    candidate = base
    if session.query(User).filter(User.username == candidate).first() is None:
        return candidate
    candidate = f"{base}_{provider}"
    n = 1
    while session.query(User).filter(User.username == candidate).first() is not None:
        n += 1
        candidate = f"{base}_{provider}{n}"
    return candidate


def resolve(result: AuthResult) -> Optional[AuthResult]:
    """Link ``result`` to a local User (creating/linking as needed).

    Returns a rewritten ``AuthResult`` carrying the LOCAL profile's username,
    system-flag roles and extra_permissions — the identity every session/token
    is built from. Returns the input unchanged (best effort) if the DB is not
    ready, so login never hard-fails on linking.
    """
    if not db_instance.SessionLocal:
        return result

    provider = result.provider or "local"
    stable_id = (result.provider_user_id or result.username or "").strip()
    if not stable_id:
        log.warning(f"IDENTITY: provider '{provider}' returned no stable id — using username.")
        stable_id = result.username

    try:
        with db_instance.SessionLocal() as s:
            identity = (
                s.query(UserIdentity)
                .filter(
                    UserIdentity.provider == provider,
                    UserIdentity.provider_user_id == stable_id,
                )
                .first()
            )

            user: Optional[User] = None
            if identity is not None:
                # Fast path — subsequent logins. Stable across upstream
                # username/email changes.
                user = s.query(User).filter(User.id == identity.user_id).first()
                if user is None:
                    log.warning(
                        f"IDENTITY: orphaned identity row for {provider}/{stable_id} — relinking."
                    )
                    s.delete(identity)
                    identity = None

            if user is None:
                user = _match_or_create(s, result, provider, stable_id)
                if user is None:
                    return result

            if identity is None:
                # last_login_at is set by the column default on insert.
                matched_by = getattr(user, "_matched_by", None) or "manual"
                s.add(UserIdentity(
                    user_id=user.id,
                    provider=provider,
                    provider_user_id=stable_id,
                    matched_by=matched_by,
                    email_at_link=(result.email or None),
                ))
            else:
                # Touch last_login_at (onupdate fires on any change).
                identity.matched_by = identity.matched_by  # no-op keeps row clean
                from datetime import datetime
                identity.last_login_at = datetime.utcnow()

            # Backfill profile fields the local row is missing.
            if result.full_name and not (user.full_name or "").strip():
                user.full_name = result.full_name
            if result.email and not (user.email or "").strip():
                user.email = result.email

            # Legacy LDAP role-mapping compatibility: historically the LDAP
            # provider folded directory→role_mapping values into result.roles and
            # those became group memberships. Kept as-is so existing
            # LYNDRIX_LDAP_ROLE_MAPPING / default-roles setups don't regress.
            if provider == "ldap" and result.roles:
                current = set(user.groups or [])
                mapped = {r for r in result.roles if r}
                added = sorted(mapped - current)
                if added:
                    user.groups = sorted(current | mapped)
                    log.info(f"IDENTITY: LDAP role-mapping membership for '{user.username}': +{added}")

            # Directory group → local group membership (the clean channel): any
            # provider may resolve directory groups to local Lyndrix group NAMES
            # in result.groups (LDAP: LYNDRIX_LDAP_GROUP_MAPPING + each group's
            # ldap_mappings). Union additively — leaving a directory group does
            # not auto-remove local membership.
            if result.groups:
                current = set(user.groups or [])
                mapped = {g for g in result.groups if g}
                added = sorted(mapped - current)
                if added:
                    user.groups = sorted(current | mapped)
                    log.info(
                        f"IDENTITY: directory-group membership for '{user.username}' "
                        f"({provider}): +{added}"
                    )

            s.commit()

            from . import access_service
            access_service.invalidate_cache(str(user.username))

            return AuthResult(
                username=str(user.username),
                full_name=str(user.full_name or result.full_name or ""),
                email=str(user.email or result.email or ""),
                roles=list(user.roles or []),          # system flags only
                provider=provider,
                provider_user_id=stable_id,
                extra_permissions=list(user.extra_permissions or []),
                email_verified=result.email_verified,
            )
    except Exception as exc:  # pragma: no cover - defensive: login must not 500
        log.error(f"IDENTITY: linking failed for {provider}/{result.username}: {exc}", exc_info=True)
        return result


def _match_or_create(session, result: AuthResult, provider: str, stable_id: str) -> Optional[User]:
    """Auto-match an existing local user or create a fresh one.

    Tags the chosen user object with ``_matched_by`` (transient attribute) so
    the caller can record how the link came to be.
    """
    # 1. Trusted email → exactly one local user without an identity for this provider.
    email = (result.email or "").strip().lower()
    if email and result.email_verified:
        matches = [
            u for u in session.query(User).all()
            if (u.email or "").strip().lower() == email
        ]
        if len(matches) == 1:
            candidate = matches[0]
            existing = (
                session.query(UserIdentity)
                .filter(
                    UserIdentity.user_id == candidate.id,
                    UserIdentity.provider == provider,
                )
                .first()
            )
            if existing is None:
                candidate._matched_by = "email"
                log.info(
                    f"IDENTITY: linked {provider}/{stable_id} to existing user "
                    f"'{candidate.username}' by verified email."
                )
                return candidate
        elif len(matches) > 1:
            log.warning(
                f"IDENTITY: email '{email}' matches {len(matches)} local users — "
                f"NOT auto-linking {provider}/{stable_id}; creating a separate account."
            )

    # 2. Local provider: the row IS the account.
    if provider == "local":
        user = session.query(User).filter(User.username == result.username).first()
        if user is not None:
            user._matched_by = "username"
            return user

    # 3. Create a brand-new user (SSO-only: no local password).
    username = _unique_username(session, result.username, provider)
    default_groups = [
        g for g in _DEFAULT_SSO_GROUPS
        if session.query(Group).filter(Group.name == g).first() is not None
    ]
    user = User(
        username=username,
        full_name=result.full_name or username,
        email=result.email or None,
        hashed_password=None,
        roles=[],
        groups=default_groups,
        extra_permissions=[],
    )
    session.add(user)
    session.flush()  # assign the UUID before the identity row references it
    user._matched_by = "new"
    log.info(
        f"IDENTITY: created local profile '{username}' for {provider}/{stable_id} "
        f"(groups={default_groups})."
    )
    return user
