import base64
import hashlib
import secrets
import time
from typing import Optional, List
from urllib.parse import urlencode

import httpx

from core.logger import get_logger
from .base import AuthProvider, AuthResult

log = get_logger("Auth:OIDCProvider")

# How long (seconds) a state token remains valid waiting for the browser redirect
_STATE_TTL = 300


class OIDCProvider(AuthProvider):
    """
    OpenID Connect / OAuth2 provider with first-class Authentik support.

    Implements the Authorization Code Flow:
      1. build_login_url()  → user is redirected to the OIDC provider (Authentik, etc.)
      2. /auth/callback/oidc receives code + state from the provider
      3. exchange_code()    → exchanges the code for tokens, fetches userinfo
      4. /auth/complete     → NiceGUI page calls consume_result() to set the session

    Required env vars (or Settings UI → Vault):
      LYNDRIX_OIDC_ISSUER         e.g. https://auth.example.com/application/o/lyndrix
      LYNDRIX_OIDC_CLIENT_ID
      LYNDRIX_OIDC_CLIENT_SECRET
      LYNDRIX_OIDC_REDIRECT_URI   e.g. https://lyndrix.example.com/auth/callback/oidc

    Optional env vars:
      LYNDRIX_OIDC_SCOPES         default: openid profile email
      LYNDRIX_OIDC_ROLE_CLAIM     default: groups  (claim name in userinfo that holds groups)
      LYNDRIX_OIDC_ADMIN_GROUPS   comma-separated group names that receive admin+superadmin roles
      LYNDRIX_OIDC_DISPLAY_NAME   button label, e.g. "Authentik" or "Corporate SSO"
    """

    provider_id = "oidc"
    display_name = "SSO"

    def __init__(
        self,
        issuer: str,
        client_id: str,
        client_secret: str,
        redirect_uri: str,
        scopes: str = "openid profile email",
        role_claim: str = "groups",
        admin_groups: Optional[List[str]] = None,
        display_name_override: str = "",
    ):
        self.issuer = issuer.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.scopes = scopes
        self.role_claim = role_claim
        self.admin_groups: List[str] = admin_groups or []
        if display_name_override:
            self.display_name = display_name_override

        self._discovery: Optional[dict] = None
        self._discovery_fetched_at: float = 0.0

        # TODO: bind OIDC state/result storage to a browser session nonce and PKCE verifier.
        self._pending_states: dict[str, float] = {}
        self._pending_results: dict[str, AuthResult] = {}
        # PKCE code_verifier per state + per-flow metadata (Identity 2.0:
        # the React SPA and the NiceGUI shell share this provider; the
        # callback needs to know which frontend started the flow).
        self._state_verifiers: dict[str, str] = {}
        self._state_meta: dict[str, dict] = {}

    # ------------------------------------------------------------------
    # AuthProvider interface
    # ------------------------------------------------------------------

    def is_configured(self) -> bool:
        return bool(
            self.issuer
            and self.client_id
            and self.client_secret
            and self.redirect_uri
        )

    def is_sso(self) -> bool:
        return True

    async def authenticate(self, username: str, password: str) -> Optional[AuthResult]:
        # OIDC does not support direct credential exchange
        return None

    # ------------------------------------------------------------------
    # OIDC Discovery
    # ------------------------------------------------------------------

    async def _get_discovery(self) -> dict:
        """Fetch and cache the OIDC discovery document (TTL: 1 hour)."""
        now = time.monotonic()
        if self._discovery and (now - self._discovery_fetched_at) < 3600:
            return self._discovery

        url = f"{self.issuer}/.well-known/openid-configuration"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            self._discovery = resp.json()
            self._discovery_fetched_at = now
            log.debug(f"AUTH:OIDC: Discovery document loaded from {url}.")
        return self._discovery

    async def probe_discovery(self) -> tuple[bool, str]:
        """Test discovery endpoint reachability — used by the settings UI."""
        if not self.is_configured():
            return False, "Provider not fully configured"
        try:
            doc = await self._get_discovery()
            issuer = doc.get("issuer", "?")
            return True, f"Issuer: {issuer}"
        except Exception as e:
            return False, str(e)

    # ------------------------------------------------------------------
    # Authorization URL
    # ------------------------------------------------------------------

    def _new_state(self) -> str:
        """Generate a cryptographically random state token and register it."""
        self._clean_expired_states()
        state = secrets.token_urlsafe(32)
        self._pending_states[state] = time.monotonic()
        return state

    def _clean_expired_states(self) -> None:
        now = time.monotonic()
        expired = [k for k, ts in self._pending_states.items() if now - ts > _STATE_TTL]
        for k in expired:
            self._pending_states.pop(k, None)
            self._pending_results.pop(k, None)
            self._state_verifiers.pop(k, None)
            self._state_meta.pop(k, None)

    def state_meta(self, state: str) -> dict:
        """Flow metadata recorded when the authorization URL was built."""
        return dict(self._state_meta.get(state) or {})

    async def build_login_url(self, flow: str = "nicegui", redirect: str = "/") -> Optional[str]:
        """Ensure discovery is loaded, then return a fresh authorization URL.

        Includes PKCE (S256 code challenge) — the verifier is bound to the
        state and sent with the token exchange. ``flow``/``redirect`` are
        remembered per state so the shared callback can hand the user back to
        the frontend that started the flow.
        """
        if not self.is_configured():
            return None
        try:
            discovery = await self._get_discovery()
        except Exception as e:
            log.error(f"AUTH:OIDC: Cannot build login URL — discovery failed: {e}")
            return None

        auth_endpoint = discovery.get("authorization_endpoint", "")
        if not auth_endpoint:
            log.error("AUTH:OIDC: Discovery document missing authorization_endpoint.")
            return None

        state = self._new_state()
        # PKCE: verifier bound to this state; S256 challenge in the URL.
        verifier = secrets.token_urlsafe(48)
        challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        self._state_verifiers[state] = verifier
        self._state_meta[state] = {"flow": flow, "redirect": redirect or "/"}

        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": self.redirect_uri,
            "scope": self.scopes,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        return f"{auth_endpoint}?{urlencode(params)}"

    # ------------------------------------------------------------------
    # Authorization Code Exchange
    # ------------------------------------------------------------------

    async def exchange_code(self, code: str, state: str) -> Optional[AuthResult]:
        """
        Exchange the authorization code for tokens, fetch userinfo, build AuthResult.
        The result is stored under the state key; retrieve it with consume_result().
        """
        self._clean_expired_states()

        if state not in self._pending_states:
            log.warning("AUTH:OIDC: Received callback with unknown or expired state.")
            return None

        try:
            discovery = await self._get_discovery()
        except Exception as e:
            log.error(f"AUTH:OIDC: Discovery unavailable during callback: {e}")
            return None

        token_endpoint = discovery.get("token_endpoint")
        userinfo_endpoint = discovery.get("userinfo_endpoint")

        if not token_endpoint or not userinfo_endpoint:
            log.error("AUTH:OIDC: Discovery document missing token/userinfo endpoints.")
            return None

        try:
            async with httpx.AsyncClient(timeout=15) as client:
                # Exchange code → tokens
                token_data = {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": self.redirect_uri,
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                }
                # PKCE: prove possession of the verifier bound to this state.
                verifier = self._state_verifiers.pop(state, None)
                if verifier:
                    token_data["code_verifier"] = verifier
                token_resp = await client.post(token_endpoint, data=token_data)
                token_resp.raise_for_status()
                tokens = token_resp.json()

                access_token = tokens.get("access_token")
                if not access_token:
                    log.error("AUTH:OIDC: Token response contained no access_token.")
                    return None

                # Fetch user info
                userinfo_resp = await client.get(
                    userinfo_endpoint,
                    headers={"Authorization": f"Bearer {access_token}"},
                )
                userinfo_resp.raise_for_status()
                userinfo = userinfo_resp.json()

        except httpx.HTTPStatusError as e:
            log.error(
                f"AUTH:OIDC: HTTP error during token exchange: {e.response.status_code}",
                exc_info=True,
            )
            return None
        except Exception as e:
            log.error(f"AUTH:OIDC: Token exchange failed: {e}", exc_info=True)
            return None

        # Map OIDC claims to AuthResult
        username = userinfo.get("preferred_username") or userinfo.get("sub", "oidc_user")
        full_name = userinfo.get("name") or username
        email = userinfo.get("email", "")
        raw_groups = userinfo.get(self.role_claim, [])
        if isinstance(raw_groups, str):
            raw_groups = [raw_groups]
        roles = self._groups_to_roles([str(g) for g in raw_groups])

        result = AuthResult(
            username=username,
            full_name=full_name,
            email=email,
            roles=roles,
            provider=self.provider_id,
            provider_user_id=userinfo.get("sub"),
            # Only an explicitly verified claim gates trusted-email
            # auto-linking — a missing/false claim never merges accounts.
            email_verified=bool(userinfo.get("email_verified") is True),
        )

        # Invalidate the state and store the result for /auth/complete to consume
        del self._pending_states[state]
        self._pending_results[state] = result

        log.info(
            f"AUTH:OIDC: Login successful for '{username}' "
            f"(sub={userinfo.get('sub')}, roles={roles})."
        )
        return result

    def consume_result(self, state: str) -> Optional[AuthResult]:
        """Pop and return the pending AuthResult. Returns None if already consumed or unknown."""
        return self._pending_results.pop(state, None)

    # ------------------------------------------------------------------
    # Role mapping
    # ------------------------------------------------------------------

    def _groups_to_roles(self, groups: List[str]) -> List[str]:
        roles: set = {"user"}
        for group in groups:
            if group in self.admin_groups:
                roles.update(["admin", "superadmin"])
        return sorted(roles)
