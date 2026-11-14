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

Linking fails CLOSED: when the link cannot be committed, ``resolve`` raises
``IdentityLinkError`` and the caller denies the login. Handing back the
provider's raw result instead would let the login continue under the EXTERNAL
username — which may be the name of an unrelated local account (``admin``).

All functions are synchronous DB work — callers on the event loop must wrap
them in ``asyncio.to_thread`` (the provider registry does).
"""
from __future__ import annotations

import hashlib
import os
from datetime import datetime
from typing import Optional

from core.logger import get_logger
from core.components.database.logic.db_service import db_instance
from core.components.database.logic.tx import run_with_retry, write_session
from .models import Group, User, UserIdentity, email_is_trusted
from .providers.base import AuthResult

log = get_logger("Core:IdentityLink")

_DEFAULT_SSO_GROUPS = [
    g.strip()
    for g in os.getenv("LYNDRIX_SSO_DEFAULT_GROUPS", "INT_VIEWER").split(",")
    if g.strip()
]

# Column sizes of users / user_identities (models.py). Provider claims are
# attacker-influenced input: a value longer than its column is a hard error on
# MariaDB in strict mode (1406), so every claim is fitted BEFORE the insert.
_USERNAME_MAX = 50
_FULL_NAME_MAX = 100
_EMAIL_MAX = 100
_PROVIDER_MAX = 20
_STABLE_ID_MAX = 255

# A first login racing another one (same account, or two accounts wanting the
# same username) loses with a duplicate-key error; the next pass finds the
# winner's rows. Transient lock errors (1020/1205/1213) are retried the same way.
_LINK_ATTEMPTS = 3


class IdentityLinkError(RuntimeError):
    """The login could not be bound to a local profile — the caller MUST deny it."""


def _fit_stable_id(raw: str) -> str:
    """The provider's account id, fitted to the column without losing uniqueness.

    Truncating would map two long ids (LDAP DNs) that share a prefix onto ONE
    identity row, i.e. onto one account — an over-long id is hashed instead.
    """
    if len(raw) <= _STABLE_ID_MAX:
        return raw
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _storable_email(email: Optional[str]) -> Optional[str]:
    """The email as it may be stored on a profile, or ``None``.

    An address that does not fit the column is dropped, never truncated: a cut
    address is a different address, and profile emails feed auto-linking.
    """
    value = (email or "").strip()
    if not value or len(value) > _EMAIL_MAX:
        return None
    return value


def _unique_username(session, wanted: str, provider: str) -> str:
    """Return ``wanted`` if free, else a provider-suffixed/numbered variant."""
    base = (wanted or f"{provider}_user").strip()[:40] or f"{provider}_user"
    candidate = base
    if session.query(User).filter(User.username == candidate).first() is None:
        return candidate
    n = 1
    while True:
        # Cut the base, never the suffix, so every variant fits the column.
        tail = f"_{provider[:8]}{n if n > 1 else ''}"
        candidate = base[: _USERNAME_MAX - len(tail)] + tail
        if session.query(User).filter(User.username == candidate).first() is None:
            return candidate
        n += 1


def resolve(result: AuthResult) -> AuthResult:
    """Link ``result`` to a local User (creating/linking as needed).

    Returns a rewritten ``AuthResult`` carrying the LOCAL profile's username,
    system-flag roles and extra_permissions plus ``linked_user_id`` — the
    identity every session/token is built from.

    Raises ``IdentityLinkError`` when the link cannot be committed (database
    not ready, constraint or lock error that does not resolve on retry, …).
    """
    if not db_instance.SessionLocal:
        raise IdentityLinkError("database not ready — cannot link the login to a local profile")

    provider = (result.provider or "local").strip()[:_PROVIDER_MAX] or "local"
    raw_id = (result.provider_user_id or "").strip()
    if not raw_id:
        log.warning(f"IDENTITY: provider '{provider}' returned no stable id — using username.")
        raw_id = (result.username or "").strip()
    if not raw_id:
        raise IdentityLinkError(f"provider '{provider}' returned neither an account id nor a username")
    stable_id = _fit_stable_id(raw_id)

    try:
        # Each pass opens its own session: after a lost insert race the fresh
        # transaction sees the winner's committed rows and takes the fast path.
        return run_with_retry(
            lambda: _resolve_once(result, provider, stable_id),
            attempts=_LINK_ATTEMPTS,
            base_delay=0.05,
            retry_integrity=True,
        )
    except IdentityLinkError:
        raise
    except Exception as exc:
        log.error(f"IDENTITY: linking failed for {provider}/{result.username}: {exc}", exc_info=True)
        raise IdentityLinkError(f"identity linking failed for provider '{provider}'") from exc


def _resolve_once(result: AuthResult, provider: str, stable_id: str) -> AuthResult:
    """One linking transaction; raises on any database error (the caller retries)."""
    # READ COMMITTED + short lock wait: two logins of the same account update
    # the same rows, which under snapshot isolation fails with error 1020.
    with write_session() as s:
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

        if user is None:
            user = _match_or_create(s, result, provider, stable_id)
            matched_by = getattr(user, "_matched_by", None) or "manual"
            email_at_link = (result.email or "").strip()[:_EMAIL_MAX] or None  # audit snapshot only
            if identity is None:
                # last_login_at is set by the column default on insert.
                s.add(UserIdentity(
                    user_id=user.id,
                    provider=provider,
                    provider_user_id=stable_id,
                    matched_by=matched_by,
                    email_at_link=email_at_link,
                ))
            else:
                # Re-point the orphaned row instead of delete + insert: both
                # would carry the same unique (provider, account id) in one flush.
                identity.user_id = user.id
                identity.matched_by = matched_by
                identity.email_at_link = email_at_link
                identity.last_login_at = datetime.utcnow()
        else:
            identity.last_login_at = datetime.utcnow()

        # Backfill profile fields the local row is missing.
        if result.full_name and not (user.full_name or "").strip():
            user.full_name = result.full_name.strip()[:_FULL_NAME_MAX]
        provider_email = _storable_email(result.email)
        if provider_email:
            if not (user.email or "").strip():
                user.email = provider_email
                user.email_trusted = bool(result.email_verified)
            elif (
                result.email_verified
                and user.email_trusted is False
                and user.email.strip().lower() == provider_email.lower()
            ):
                # The provider now vouches, for THIS account, for the address
                # its owner typed in — that is the proof self-service lacked.
                user.email_trusted = True

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

        # Built from the committed row — never from the provider's claims alone.
        linked = AuthResult(
            username=str(user.username),
            full_name=str(user.full_name or result.full_name or ""),
            email=str(user.email or result.email or ""),
            roles=list(user.roles or []),          # system flags only
            provider=provider,
            provider_user_id=stable_id,
            extra_permissions=list(user.extra_permissions or []),
            email_verified=result.email_verified,
            linked_user_id=str(user.id),
        )

    from . import access_service
    access_service.invalidate_cache(linked.username)
    return linked


def _match_or_create(session, result: AuthResult, provider: str, stable_id: str) -> User:
    """Auto-match an existing local user or create a fresh one.

    Tags the chosen user object with ``_matched_by`` (transient attribute) so
    the caller can record how the link came to be.
    """
    # 1. Trusted email → exactly one local user without an identity for this provider.
    #    "Trusted" on BOTH sides: the provider vouches for the address
    #    (email_verified) AND the local profile got it from an administrator or
    #    a provider. An address the account owner typed in themselves is not
    #    proof of ownership — matching on it would hand the next person who
    #    logs in with that address (and their directory groups) to that account.
    email = (result.email or "").strip().lower()
    if email and len(email) <= _EMAIL_MAX and result.email_verified:
        matches = [
            u for u in session.query(User).all()
            if (u.email or "").strip().lower() == email
        ]
        if len(matches) == 1 and not email_is_trusted(matches[0]):
            log.warning(
                f"IDENTITY: email '{email}' matches local user '{matches[0].username}', but that "
                f"address was set by the user themselves (unverified) — NOT auto-linking "
                f"{provider}/{stable_id}; creating a separate account. An administrator can "
                f"confirm the address by saving it on the user."
            )
        elif len(matches) == 1:
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
    new_email = _storable_email(result.email)
    user = User(
        username=username,
        full_name=((result.full_name or "").strip() or username)[:_FULL_NAME_MAX],
        email=new_email,
        # Provider-supplied: trusted exactly when the provider vouches for it.
        email_trusted=bool(new_email) and bool(result.email_verified),
        hashed_password=None,
        roles=[],
        groups=default_groups,
        extra_permissions=[],
    )
    # A new profile never inherits API keys/preferences that an earlier account
    # of this name left behind (accounts deleted by an older core kept theirs).
    from .api_key_service import api_key_service

    api_key_service.purge_for_username(session, username)
    session.add(user)
    session.flush()  # assign the UUID before the identity row references it
    user._matched_by = "new"
    log.info(
        f"IDENTITY: created local profile '{username}' for {provider}/{stable_id} "
        f"(groups={default_groups})."
    )
    return user
