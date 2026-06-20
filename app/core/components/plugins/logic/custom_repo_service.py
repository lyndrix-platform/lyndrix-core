"""Service for user-added custom plugin repositories.

Each row is one Git repo (== one plugin) the operator added manually. These are
merged into the marketplace (see :meth:`PluginService.fetch_marketplace_data`)
and installed through the same path as curated collection plugins.

The optional per-repository API token is stored in Vault (never the DB) under
``core/plugin_repos`` with key ``repo_{id}_token``. The DB row only flags
``has_token``; token resolution (per-repo override → global provider token →
anonymous) lives in :mod:`plugin_service`.
"""

from typing import List, Optional

from core.logger import get_logger
from core.components.database.logic.db_service import db_instance
from .models import CustomPluginRepository
from . import git_providers

log = get_logger("Core:CustomRepoService")

VAULT_MOUNT = "lyndrix"
VAULT_PATH = "core/plugin_repos"


def _token_key(repo_id: int) -> str:
    return f"repo_{repo_id}_token"


class CustomRepositoryService:
    def __init__(self):
        self._schema_ready = False

    # ------------------------------------------------------------------ schema
    def ensure_schema(self) -> None:
        """Create the custom_plugin_repositories table if it does not exist.

        Mirrors ``ModuleManager._ensure_plugin_state_schema`` — tables are
        created per-model on ``db:connected`` rather than via a global
        ``create_all``.
        """
        if self._schema_ready or not db_instance.engine:
            return
        try:
            with db_instance.engine.begin() as connection:
                CustomPluginRepository.__table__.create(bind=connection, checkfirst=True)
            self._schema_ready = True
            log.info("CUSTOM_REPO: Schema ready.")
        except Exception as exc:
            log.error(f"CUSTOM_REPO: Failed to ensure schema: {exc}")

    # ------------------------------------------------------------------- vault
    def _vault(self):
        try:
            from core.services import vault_instance
            if vault_instance.is_connected:
                return vault_instance
        except Exception:
            pass
        return None

    def _read_vault_data(self, vault) -> dict:
        try:
            resp = vault.client.secrets.kv.v2.read_secret_version(
                path=VAULT_PATH, mount_point=VAULT_MOUNT
            )
            return resp["data"]["data"] or {}
        except Exception:
            return {}

    def _write_token(self, repo_id: int, token: str) -> bool:
        vault = self._vault()
        if not vault:
            log.warning("CUSTOM_REPO: Vault not connected — token not stored.")
            return False
        data = self._read_vault_data(vault)
        data[_token_key(repo_id)] = token
        try:
            vault.client.secrets.kv.v2.create_or_update_secret(
                path=VAULT_PATH, mount_point=VAULT_MOUNT, secret=data
            )
            return True
        except Exception as exc:
            log.error(f"CUSTOM_REPO: Failed to write token: {exc}")
            return False

    def _delete_token(self, repo_id: int) -> None:
        vault = self._vault()
        if not vault:
            return
        data = self._read_vault_data(vault)
        if data.pop(_token_key(repo_id), None) is not None:
            try:
                vault.client.secrets.kv.v2.create_or_update_secret(
                    path=VAULT_PATH, mount_point=VAULT_MOUNT, secret=data
                )
            except Exception as exc:
                log.error(f"CUSTOM_REPO: Failed to delete token: {exc}")

    def get_token(self, repo_id: int) -> Optional[str]:
        """Read a per-repo token from Vault, or None."""
        vault = self._vault()
        if not vault:
            return None
        return self._read_vault_data(vault).get(_token_key(repo_id)) or None

    # --------------------------------------------------------------------- CRUD
    def list_all(self) -> List[CustomPluginRepository]:
        if not db_instance.SessionLocal:
            return []
        self.ensure_schema()
        with db_instance.SessionLocal() as s:
            return s.query(CustomPluginRepository).order_by(CustomPluginRepository.name).all()

    def list_enabled(self) -> List[CustomPluginRepository]:
        return [r for r in self.list_all() if r.enabled]

    def get(self, repo_id: int) -> Optional[CustomPluginRepository]:
        if not db_instance.SessionLocal:
            return None
        with db_instance.SessionLocal() as s:
            return s.query(CustomPluginRepository).filter(
                CustomPluginRepository.id == repo_id
            ).first()

    def get_by_url(self, repo_url: str) -> Optional[CustomPluginRepository]:
        if not db_instance.SessionLocal:
            return None
        normalized = self._normalize_url(repo_url)
        with db_instance.SessionLocal() as s:
            return s.query(CustomPluginRepository).filter(
                CustomPluginRepository.repo_url == normalized
            ).first()

    @staticmethod
    def _normalize_url(repo_url: str) -> str:
        return repo_url.strip().rstrip("/").removesuffix(".git")

    def create(
        self,
        repo_url: str,
        name: Optional[str] = None,
        description: Optional[str] = None,
        token: Optional[str] = None,
    ) -> CustomPluginRepository:
        """Add a custom repository. Raises ValueError on invalid/duplicate URL."""
        if not db_instance.SessionLocal:
            raise ValueError("Database not connected.")
        self.ensure_schema()

        normalized = self._normalize_url(repo_url)
        if not normalized:
            raise ValueError("Repository URL is required.")

        # Validate + derive metadata from the URL.
        ref = git_providers.parse_repo(normalized)
        provider = ref.provider
        display_name = (name or "").strip() or ref.repo.replace("-", " ").title()

        with db_instance.SessionLocal() as s:
            existing = s.query(CustomPluginRepository).filter(
                CustomPluginRepository.repo_url == normalized
            ).first()
            if existing:
                raise ValueError("This repository has already been added.")

            row = CustomPluginRepository(
                name=display_name,
                repo_url=normalized,
                provider=provider,
                description=(description or "").strip() or None,
                enabled=True,
                has_token=False,
            )
            s.add(row)
            s.commit()
            s.refresh(row)
            repo_id = row.id

        if token and token.strip():
            if self._write_token(repo_id, token.strip()):
                with db_instance.SessionLocal() as s:
                    row = s.query(CustomPluginRepository).filter(
                        CustomPluginRepository.id == repo_id
                    ).first()
                    row.has_token = True
                    s.commit()
                    s.refresh(row)

        self._invalidate_marketplace_cache()
        log.info(f"CUSTOM_REPO: Added '{display_name}' ({normalized}) [{provider}].")
        return self.get(repo_id)

    def set_token(self, repo_id: int, token: Optional[str]) -> bool:
        """Set (or clear, when ``token`` is falsy) the per-repo token."""
        row = self.get(repo_id)
        if not row:
            return False
        if token and token.strip():
            ok = self._write_token(repo_id, token.strip())
            has = ok
        else:
            self._delete_token(repo_id)
            ok, has = True, False
        with db_instance.SessionLocal() as s:
            db_row = s.query(CustomPluginRepository).filter(
                CustomPluginRepository.id == repo_id
            ).first()
            if db_row:
                db_row.has_token = has
                s.commit()
        return ok

    def delete(self, repo_id: int) -> bool:
        if not db_instance.SessionLocal:
            return False
        with db_instance.SessionLocal() as s:
            row = s.query(CustomPluginRepository).filter(
                CustomPluginRepository.id == repo_id
            ).first()
            if not row:
                return False
            s.delete(row)
            s.commit()
        self._delete_token(repo_id)
        self._invalidate_marketplace_cache()
        log.info(f"CUSTOM_REPO: Removed repository id={repo_id}.")
        return True

    # ---------------------------------------------------------------- internal
    def _invalidate_marketplace_cache(self) -> None:
        """Clear the marketplace cache so changes show on the next fetch."""
        try:
            from .plugin_service import plugin_service
            plugin_service._marketplace_cache = []
            plugin_service._cache_timestamp = 0
        except Exception:
            pass


custom_repo_service = CustomRepositoryService()
