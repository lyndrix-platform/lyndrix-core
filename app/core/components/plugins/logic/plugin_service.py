import ast
import json
import os
import sys
import shutil
import asyncio
import zipfile
import time
import tempfile
import subprocess
import httpx
import re
from pathlib import Path
from config import settings
from core.logger import get_logger
from core.bus import bus
from . import git_providers

log = get_logger("Core:PluginService")

# --- Plugin collection source config ---
# The collection is cloned locally by the git-manager plugin.
# git-manager stores repos under /data/storage/git_repos/{repo_id}.
# Source URL is configurable via LYNDRIX_PLUGIN_COLLECTION_URL (defaults to the
# lyndrix-platform org); see config.Settings.
COLLECTION_REPO_URL = settings.LYNDRIX_PLUGIN_COLLECTION_URL
COLLECTION_REPO_ID = COLLECTION_REPO_URL.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")
COLLECTION_LOCAL_PATH = Path("/data/storage/git_repos") / COLLECTION_REPO_ID
COLLECTION_JSON_PATH = COLLECTION_LOCAL_PATH / "plugin-directory" / "plugins.json"


def _raw_collection_fallback_url(git_url: str) -> str:
    """Derive the raw GitHub plugins.json URL from the collection git URL."""
    base = git_url.rstrip("/").removesuffix(".git")
    if base.startswith("https://github.com/"):
        owner_repo = base[len("https://github.com/"):]
        return (
            f"https://raw.githubusercontent.com/{owner_repo}"
            "/main/plugin-directory/plugins.json"
        )
    return base

# HTTP fallback (used when the local clone is not yet available).
# Override via PLUGIN_COLLECTION_URL env var, else derived from the collection URL.
COLLECTION_FALLBACK_URL = os.environ.get(
    "PLUGIN_COLLECTION_URL",
    _raw_collection_fallback_url(COLLECTION_REPO_URL),
)

# How often to check for new commits on main (seconds).
COLLECTION_POLL_INTERVAL = int(os.environ.get("PLUGIN_COLLECTION_POLL_INTERVAL", "900"))  # 15 min

class PluginService:
    def __init__(self):
        # FIX: Robust path finding
        # app/core/components/plugins/logic/plugin_service.py -> app/plugins
        self.plugin_dir = Path(__file__).parents[4] / "plugins"
        self.plugin_dir.mkdir(parents=True, exist_ok=True)
        self.github_api_base = "https://api.github.com/repos"
        
        # Cache für Marketplace-Daten
        self._marketplace_cache = []
        self._cache_timestamp = 0
        self._tag_cache = {}
        self._tag_cache_timestamp = {}
        # Version tags shipped in the plugin-collection index, keyed by
        # normalized repo URL. Refreshed whenever the collection is loaded; used
        # to populate version pickers without hitting GitHub on every click.
        self._collection_versions = {}
        self._cache_ttl = 900  # 15 Minuten Cache-Dauer
        # Negative cache: after a failed collection fetch, skip ALL remote
        # retries until this deadline. Without it every /plugins open paid the
        # full HTTP-fallback timeout again while no collection clone exists.
        self._negative_cache_until = 0.0
        self._repo_cache = {}
        self._repo_cache_timestamp = {}
        # Tracks the last known remote HEAD commit to avoid unnecessary git pulls.
        self._last_known_head: str | None = None
        self._watcher_task: asyncio.Task | None = None

    def _extract_repo_info(self, github_url: str):
        parts = github_url.rstrip("/").split("/")
        if len(parts) >= 2:
            repo = parts[-1]
            if repo.endswith(".git"):
                repo = repo[:-4]
            return parts[-2], repo
        raise ValueError("Invalid GitHub URL format")

    @staticmethod
    def _extract_manifest_field(entrypoint_path: str, field: str) -> str | None:
        """Statically extract a string field of the ``ModuleManifest(...)`` call
        in an entrypoint.py by parsing the source — never executing it.

        Plugin entrypoints declare their manifest via a top-level
        ``manifest = ModuleManifest(id="...", version="...", ...)`` call and
        routinely use relative imports, so executing the module standalone fails.
        AST parsing avoids that and is also safer: we read the identity/version of
        untrusted code without running it. Returns the value when it is declared
        as a string literal, else ``None``.
        """
        try:
            source = Path(entrypoint_path).read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError, ValueError) as e:
            log.error(f"Failed to parse entrypoint '{entrypoint_path}': {e}")
            return None

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            func_name = (
                func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name)
                else None
            )
            if func_name != "ModuleManifest":
                continue
            for kw in node.keywords:
                if kw.arg == field and isinstance(kw.value, ast.Constant) \
                        and isinstance(kw.value.value, str):
                    return kw.value.value
        return None

    @staticmethod
    def _extract_manifest_id(entrypoint_path: str) -> str | None:
        """Statically extract ``manifest.id`` (see ``_extract_manifest_field``)."""
        return PluginService._extract_manifest_field(entrypoint_path, "id")

    def _read_manifest_id(self, plugin_path: Path) -> str | None:
        """Best-effort read of manifest.id from an entrypoint.py without
        importing or executing the module."""
        entrypoint_path = plugin_path / "entrypoint.py"
        if not entrypoint_path.exists():
            return None
        return self._extract_manifest_id(str(entrypoint_path))

    def _read_manifest_version(self, plugin_path: Path) -> str | None:
        """Best-effort read of manifest.version from a plugin dir (no import)."""
        entrypoint_path = plugin_path / "entrypoint.py"
        if not entrypoint_path.exists():
            return None
        return self._extract_manifest_field(str(entrypoint_path), "version")

    # ------------------------------------------------------------------
    # Rollback / restore-point helpers
    # ------------------------------------------------------------------
    def _previous_path(self, safe_repo_name: str) -> Path:
        return self.plugin_dir / f".previous_{safe_repo_name}"

    def has_previous(self, safe_repo_name: str) -> bool:
        """True when a retained previous version exists to revert to."""
        return self._previous_path(safe_repo_name).exists()

    def previous_version(self, safe_repo_name: str) -> str | None:
        """Version label of the retained previous version, if any."""
        prev = self._previous_path(safe_repo_name)
        return self._read_manifest_version(prev) if prev.exists() else None

    def restore_previous(self, safe_repo_name: str) -> bool:
        """Swap the retained ``.previous_<repo>`` back into place, discarding the
        current (failed/unwanted) version. Used by auto-revert and manual
        rollback. Returns True on success."""
        plugin_path = self.plugin_dir / safe_repo_name
        previous_path = self._previous_path(safe_repo_name)
        if not previous_path.exists():
            log.warning(f"ROLLBACK: No previous version retained for '{safe_repo_name}'.")
            return False
        failed_path = self.plugin_dir / f".failed_{safe_repo_name}"
        try:
            if failed_path.exists():
                shutil.rmtree(failed_path, ignore_errors=True)
            if plugin_path.exists():
                plugin_path.rename(failed_path)
            previous_path.rename(plugin_path)
            shutil.rmtree(failed_path, ignore_errors=True)
            log.info(f"ROLLBACK: Restored previous version of '{safe_repo_name}'.")
            return True
        except Exception as exc:
            log.error(f"ROLLBACK: Failed to restore previous '{safe_repo_name}': {exc}", exc_info=True)
            # Best effort: if current was moved out but restore failed, put it back.
            if not plugin_path.exists() and failed_path.exists():
                failed_path.rename(plugin_path)
            return False

    async def rollback_plugin(self, module_id: str, repo_name: str) -> bool:
        """Operator-triggered one-click revert to the retained previous version.
        Restores files, asks the ModuleManager to reload, and records history."""
        safe_repo_name = repo_name.replace("-", "_")
        prev_version = self.previous_version(safe_repo_name)
        if not self.restore_previous(safe_repo_name):
            bus.emit("plugin:install_failed", {"repo": repo_name, "error": "no_previous_version"})
            return False
        self._record_history(module_id, repo_name, prev_version, action="rollback", outcome="ok")
        # Reload the restored version and announce the revert.
        bus.emit("plugin:files_changed", {"action": "install", "name": safe_repo_name})
        bus.emit("plugin:rolled_back", {"repo": repo_name, "module_id": module_id, "version": prev_version})
        log.info(f"ROLLBACK: '{repo_name}' reverted to previous version {prev_version}.")
        return True

    def _record_history(self, module_id, repo_name, version, *, action, outcome,
                        previous_version=None, error=None):
        """Append one audit row to plugin_version_history (best-effort)."""
        try:
            from core.components.database.logic.db_service import db_instance
            from .models import PluginVersionHistory
            if not db_instance.engine:
                return
            PluginVersionHistory.__table__.create(bind=db_instance.engine, checkfirst=True)
            with db_instance.SessionLocal() as session:
                session.add(PluginVersionHistory(
                    module_id=module_id, repo_name=repo_name, version=version,
                    previous_version=previous_version, action=action, outcome=outcome,
                    error=(error[:1000] if isinstance(error, str) else error),
                ))
                session.commit()
        except Exception as exc:
            log.debug(f"HISTORY: could not record {action}/{outcome} for {repo_name}: {exc}")

    def _verify_plugin_identity(self, staging_path: str, expected_id: str) -> bool:
        """Verify the extracted plugin's manifest.id matches the expected
        identity, to prevent install/upgrade-time substitution attacks.

        The id is read statically (see ``_extract_manifest_id``) so plugins that
        use relative imports in their entrypoint verify correctly."""
        entrypoint_path = os.path.join(staging_path, "entrypoint.py")
        if not os.path.exists(entrypoint_path):
            log.error("No entrypoint.py found in downloaded archive.")
            return False

        actual_id = self._extract_manifest_id(entrypoint_path)
        if actual_id is None:
            log.error("Unable to read manifest id from staged entrypoint.py.")
            return False
        if actual_id != expected_id:
            log.error(
                f"Identity mismatch: expected '{expected_id}', "
                f"archive contains '{actual_id}'"
            )
            return False
        return True

    def _normalize_repo_name(self, repo_name: str) -> str:
        return repo_name.replace("-", "_")

    def _repo_aliases(self, repo_name: str):
        normalized = self._normalize_repo_name(repo_name)
        aliases = {normalized}
        # Plugins are mounted under their bare name (e.g. ``discord_notifier``)
        # while the collection lists them as ``lyndrix-plugin-discord-notifier``.
        # Add the prefix-stripped forms so installed records match.
        for prefix in ("lyndrix_plugin_", "lyndrix_", "plugin_"):
            if normalized.startswith(prefix):
                aliases.add(normalized[len(prefix):])
        return aliases

    def _read_marketplace_urls(self):
        list_file = Path(__file__).parents[4] / "assets" / "plugin-list.txt"
        if not list_file.exists():
            return []
        with open(list_file, "r", encoding="utf-8") as handle:
            return [line.strip() for line in handle.readlines() if line.strip() and not line.startswith("#")]

    def _get_remote_head(self) -> str | None:
        """Run git ls-remote to get the current HEAD of the collection repo.
        This is a lightweight network check — no clone or fetch needed.
        Returns the commit SHA, or None on failure.
        """
        try:
            result = subprocess.run(
                ["git", "ls-remote", COLLECTION_REPO_URL, "HEAD"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout:
                return result.stdout.split()[0]
        except Exception as exc:
            log.debug(f"COLLECTION: ls-remote check failed: {exc}")
        return None

    def _request_git_sync(self):
        """Emit git:sync so the git-manager plugin clones/pulls the collection."""
        log.info("COLLECTION: Requesting git-manager to sync plugin collection...")
        bus.emit("git:sync", {
            "repo_id": COLLECTION_REPO_ID,
            "url": COLLECTION_REPO_URL,
            "auth_type": "none",  # public repo — no token needed
        })

    def start_collection_watcher(self):
        """Start a background task that polls for new commits and syncs when needed.
        Call this once from the plugins component setup().
        """
        if self._watcher_task and not self._watcher_task.done():
            return
        self._watcher_task = bus.create_tracked_task(
            self._collection_watcher_loop(), name="plugin_collection_watcher"
        )
        log.info(f"COLLECTION: Watcher started (poll interval: {COLLECTION_POLL_INTERVAL}s).")

    async def _collection_watcher_loop(self):
        """Periodically check the remote HEAD and sync only when it has changed.
        The initial sync is triggered by on_boot_complete() once all plugins
        (including git-manager) are active. This loop only handles periodic polling.
        """
        while True:
            await asyncio.sleep(COLLECTION_POLL_INTERVAL)
            try:
                remote_head = await asyncio.get_event_loop().run_in_executor(
                    None, self._get_remote_head
                )
                if remote_head and remote_head != self._last_known_head:
                    log.info(
                        f"COLLECTION: New commit detected ({remote_head[:8]}). Requesting sync."
                    )
                    self._request_git_sync()
                    self._last_known_head = remote_head
                else:
                    log.debug("COLLECTION: No new commits, skipping sync.")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning(f"COLLECTION: Watcher iteration failed: {exc}")

    def on_boot_complete(self, payload: dict = None):
        """Trigger the initial git sync once all plugins (including git-manager) are active."""
        log.info("COLLECTION: Boot complete — requesting initial collection sync.")
        self._request_git_sync()

    def on_git_status_update(self, payload: dict):
        """Called when git-manager emits git:status_update.
        Invalidates the marketplace cache so the next fetch reads fresh data.
        """
        if payload.get("repo_id") != COLLECTION_REPO_ID:
            return
        if payload.get("status") == "synced":
            log.info("COLLECTION: git-manager sync complete — invalidating marketplace cache.")
            self._marketplace_cache = []
            self._cache_timestamp = 0
            # A fresh clone just arrived — drop any armed negative cache so the
            # next fetch reads it immediately instead of waiting out the window.
            self._negative_cache_until = 0.0

    def get_marketplace_source_map(self) -> dict:
        """Returns a sync map of normalized repo name -> url.
        Uses the in-memory cache populated by fetch_marketplace_data.
        Falls back to the local plugin-list.txt if the cache is empty.
        """
        if self._marketplace_cache:
            return {p["repo_safe"]: p["url"] for p in self._marketplace_cache}
        # Fallback: parse local txt file synchronously (e.g. before first async refresh)
        repo_map = {}
        for url in self._read_marketplace_urls():
            try:
                _, repo = self._extract_repo_info(url)
                for alias in self._repo_aliases(repo):
                    repo_map[alias] = url
            except ValueError:
                continue
        return repo_map

    @staticmethod
    def _norm_url_key(url: str) -> str:
        """Host/owner/repo key for matching, ignoring scheme, .git and slashes."""
        if not url:
            return ""
        key = url.strip().lower().rstrip("/")
        for prefix in ("https://", "http://", "git@"):
            if key.startswith(prefix):
                key = key[len(prefix):]
                break
        key = key.replace(":", "/")  # scp-style git@host:owner/repo
        if key.endswith(".git"):
            key = key[:-4]
        return key

    def _versions_with_latest(self, version_tags) -> list:
        """['latest'] followed by the de-duplicated version tags."""
        tags = [t for t in (version_tags or []) if t and t != "latest"]
        return ["latest"] + tags

    def _collection_entry_to_marketplace(self, entry: dict) -> dict:
        """Convert a plugins.json entry from lyndrix-plugin-collection to marketplace format."""
        html_url = entry.get("html_url", "")
        name = entry.get("name", "")
        full_name = entry.get("full_name", "")
        author = full_name.split("/")[0] if "/" in full_name else "Unknown"
        repo_safe = self._normalize_repo_name(name)
        return {
            "name": name.replace("-", " ").title(),
            # Left empty when the collection has none; the UI falls back to the
            # installed plugin's manifest description.
            "description": entry.get("description") or "",
            "stars": entry.get("stargazers_count", 0),
            "url": html_url,
            "repo_url": html_url,  # mirrors installed PluginOut.repo_url — used by the React UI to detect installed state
            "clone_url": entry.get("clone_url", html_url),
            "author": author,
            "repo_safe": repo_safe,
            "repo_aliases": sorted(self._repo_aliases(name)),
            # Version tags shipped by the collection — pre-populates the picker
            # without a GitHub call; the refresh button fetches newer ones.
            "versions": self._versions_with_latest(entry.get("version_tags")),
            "topics": entry.get("topics", []),
            "archived": entry.get("archived", False),
            "metadata_source": "collection",
        }

    def _custom_repo_to_marketplace(self, row) -> dict:
        """Convert a CustomPluginRepository row to the marketplace dict shape.

        Mirrors :meth:`_collection_entry_to_marketplace` so custom repos render,
        install, update and badge through the same code path as collection
        plugins. ``metadata_source`` is ``"custom"`` and ``custom_id`` carries
        the DB row id so the UI can offer a remove-source action.
        """
        try:
            ref = git_providers.parse_repo(row.repo_url)
            repo_name = ref.repo
            author = ref.namespace.split("/")[0] if ref.namespace else ref.host
        except ValueError:
            repo_name = row.name
            author = "Unknown"
        repo_safe = self._normalize_repo_name(repo_name)
        return {
            "name": (row.name or repo_name.replace("-", " ").title()),
            "description": row.description or "Custom repository.",
            "stars": 0,
            "url": row.repo_url,
            "repo_url": row.repo_url,  # mirrors installed PluginOut.repo_url — used by the React UI to detect installed state
            "clone_url": row.repo_url,
            "author": author,
            "repo_safe": repo_safe,
            "repo_aliases": sorted(self._repo_aliases(repo_name)),
            "versions": ["latest"],  # fetched live via the refresh button
            "topics": [],
            "archived": False,
            "metadata_source": "custom",
            "custom_id": row.id,
            "provider": row.provider,
        }

    def _get_headers(self, include_auth: bool = True) -> dict:
        """Build GitHub request headers (used for the public collection fallback).

        Provider-aware install/version requests use :meth:`_auth_headers_for`.
        """
        headers = {
            "User-Agent": "Lyndrix-Core/1.0",
            "Accept": "application/vnd.github.v3+json"
        }
        if not include_auth:
            return headers
        token = self._global_token(git_providers.GITHUB)
        if token:
            headers["Authorization"] = f"token {token}"
        return headers

    @staticmethod
    def _read_vault_setting(key: str) -> str | None:
        try:
            from core.services import vault_instance
            if vault_instance.is_connected:
                resp = vault_instance.client.secrets.kv.v2.read_secret_version(
                    path="core/settings", mount_point="lyndrix"
                )
                return resp['data']['data'].get(key)
        except Exception:
            pass
        return None

    def _global_token(self, provider: str) -> str | None:
        """Global token for a provider: env var first, then Vault core/settings."""
        if provider == git_providers.GITHUB:
            return os.environ.get("GITHUB_TOKEN") or self._read_vault_setting("github_token")
        return os.environ.get("GITLAB_TOKEN") or self._read_vault_setting("gitlab_token")

    def _resolve_token(self, url: str, provider: str) -> str | None:
        """Resolve the token for a repo URL.

        Order: per-repo override (Vault, via the custom repo row) → global
        provider token (env/Vault) → None (anonymous).
        """
        try:
            from .custom_repo_service import custom_repo_service
            row = custom_repo_service.get_by_url(url)
            if row and row.has_token:
                token = custom_repo_service.get_token(row.id)
                if token:
                    return token
        except Exception:
            pass
        return self._global_token(provider)

    def _auth_headers_for(self, url: str, provider: str) -> dict:
        """Provider-appropriate request headers, including auth when available."""
        headers = {"User-Agent": "Lyndrix-Core/1.0"}
        headers.update(git_providers.accept_header(provider))
        token = self._resolve_token(url, provider)
        if token:
            headers.update(git_providers.auth_header(provider, token))
        return headers

    def get_known_versions(self, url: str) -> list:
        """Synchronously return cached versions for a repo URL — no network.

        Prefers a recent live result (from a forced refresh) over the
        collection-shipped tags. Returns ``[]`` when nothing is cached, so the
        caller can fall back to a placeholder until the refresh button is used.
        """
        if url in self._tag_cache:
            return self._versions_with_latest(self._tag_cache[url])
        return list(self._collection_versions.get(self._norm_url_key(url), []))

    async def get_plugin_versions(self, github_url: str, force_refresh: bool = False):
        if not force_refresh:
            if github_url in self._tag_cache and (
                time.time() - self._tag_cache_timestamp.get(github_url, 0) < self._cache_ttl
            ):
                return self._tag_cache[github_url]
            # Fall back to collection-shipped tags rather than calling GitHub.
            collection = self._collection_versions.get(self._norm_url_key(github_url))
            if collection:
                return [v for v in collection if v != "latest"]

        try:
            ref = git_providers.parse_repo(github_url)
        except ValueError:
            return []

        api_url = git_providers.tags_url(ref)
        try:
            headers = self._auth_headers_for(github_url, ref.provider)
            async with httpx.AsyncClient(headers=headers, follow_redirects=True) as client:
                resp = await client.get(api_url)
                if resp.status_code == 200:
                    raw_tags = git_providers.tag_names(ref.provider, resp.json())
                    def parse_v(t):
                        c = t.lstrip('v')
                        parts = []
                        for p in re.split(r'[^0-9]+', c):
                            if p:
                                parts.append(int(p))
                        return parts
                    tag_list = sorted(raw_tags, key=parse_v, reverse=True)
                    self._tag_cache[github_url] = tag_list
                    self._tag_cache_timestamp[github_url] = time.time()
                    return tag_list
        except Exception as e:
            log.error(f"Failed to fetch tags for {github_url}: {e}")
        return []

    async def install_plugin(self, github_url: str, version: str = "latest", upgrade: bool = False):
        """Downloads, extracts and registers a new plugin from GitHub or GitLab."""
        ref = git_providers.parse_repo(github_url)
        repo = ref.repo
        provider = ref.provider

        # FIX: Python-kompatiblen Ordnernamen erzwingen (keine Bindestriche)
        safe_repo_name = repo.replace("-", "_")
        plugin_path = self.plugin_dir / safe_repo_name
        
        # Stage outside the watched plugin directory so dev reloads only happen
        # after the final move into /app/plugins.
        staging_base = Path(tempfile.mkdtemp(prefix=f"lyndrix_plugin_{safe_repo_name}_"))
        zip_path = staging_base / f"{repo}.zip"
        extracted_dir = None
        
        action_name = "UPGRADE" if upgrade else "INSTALL"
        log.info(f"{action_name}: Requesting plugin '{repo}' version '{version}' from {github_url}")
        bus.emit("plugin:install_started", {"repo": repo, "version": version})

        if plugin_path.exists() and not upgrade:
            log.warning(f"CONFLICT: Plugin directory '{safe_repo_name}' already exists. Operation aborted")
            return False

        try:
            headers = self._auth_headers_for(github_url, provider)
            async with httpx.AsyncClient(follow_redirects=True, headers=headers) as client:
                # 1. Fetch Repository Metadata (to discover the default branch)
                api_url = git_providers.metadata_url(ref)
                resp = await client.get(api_url)

                if resp.status_code == 403:
                    log.warning("INSTALL: Rate limit hit for metadata. Assuming 'main' branch.")
                    default_branch = "main"
                else:
                    resp.raise_for_status()
                    repo_info = resp.json()
                    default_branch = repo_info.get("default_branch", "main")

                # 2. Download Archive
                is_latest = version == "latest"
                ref_name = default_branch if is_latest else version
                zip_url = git_providers.archive_url(ref, ref_name, is_tag=not is_latest)

                log.info(f"DOWNLOAD: Fetching source from {zip_url}")
                response = await client.get(zip_url)

                # GitHub fallback: many repos default to 'master' instead of 'main'.
                if (
                    response.status_code == 404
                    and is_latest
                    and provider == git_providers.GITHUB
                    and default_branch == "main"
                ):
                    log.info("DOWNLOAD: 'main' not found, trying 'master'...")
                    zip_url = git_providers.archive_url(ref, "master", is_tag=False)
                    response = await client.get(zip_url)

                response.raise_for_status()

                # File write is blocking; keep it off the event loop.
                await asyncio.to_thread(zip_path.write_bytes, response.content)

            # 3. Safe Extraction into Staging (ZIP Slip protected)
            log.info("FILESYSTEM: Extracting archive into hidden staging area...")

            def _extract_archive() -> Path:
                with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                    # TODO(agent): require signed/allowlisted plugin sources before
                    # extraction and install (supply-chain hardening — CORE-AUTH-PLUGINS-008).
                    # This is a larger architectural change (signing keys + source
                    # allowlist + dependency sandboxing) deferred out of this pass;
                    # the ZIP-slip and requirements checks below are the interim guards.
                    staging_resolved = staging_base.resolve()
                    root_folder = zip_ref.namelist()[0].split('/')[0]
                    # Validate all paths stay within staging_base. Use is_relative_to
                    # (not a string prefix, which a sibling dir sharing the prefix could
                    # satisfy) so the ZIP-slip guard is correct.
                    for member in zip_ref.namelist():
                        member_path = (staging_base / member).resolve()
                        if member_path != staging_resolved and not member_path.is_relative_to(staging_resolved):
                            raise ValueError(f"SECURITY: ZIP contains path traversal entry: {member}")
                    zip_ref.extractall(staging_base)
                    return staging_base / root_folder

            # Extraction is CPU + IO heavy for large archives — run it off the loop.
            extracted_dir = await asyncio.to_thread(_extract_archive)

            # 4. Dependency Management in Staging
            await self._install_requirements(extracted_dir)

            # 4b. Identity verification — when upgrading an existing plugin we
            # know the expected manifest.id from the installed copy. Refuse the
            # swap if the freshly downloaded archive declares a different id.
            if upgrade and plugin_path.exists():
                expected_id = self._read_manifest_id(plugin_path)
                if expected_id and not self._verify_plugin_identity(
                    str(extracted_dir), expected_id
                ):
                    log.error(
                        f"SECURITY: Aborting upgrade of '{safe_repo_name}' — "
                        f"plugin identity mismatch."
                    )
                    bus.emit(
                        "plugin:install_failed",
                        {"repo": repo, "error": "identity_mismatch"},
                    )
                    return False

            # 5. ATOMIC SWAP (Protects against dev server hot-reload crashes).
            # Runs off the event loop: staging is a tempdir that may be on a
            # different filesystem than /app/plugins, so shutil.move can be a full
            # copy, and rmtree of a retained prior version is IO-heavy.
            def _swap_in_new_version():
                backup_path = self.plugin_dir / f".backup_{safe_repo_name}"
                previous_path = self._previous_path(safe_repo_name)
                prior = self._read_manifest_version(plugin_path) if plugin_path.exists() else None
                if plugin_path.exists():
                    log.info(f"UPGRADE: Swapping old directory {plugin_path} for new version...")
                    if backup_path.exists():
                        shutil.rmtree(backup_path, ignore_errors=True)
                    plugin_path.rename(backup_path)

                shutil.move(str(extracted_dir), str(plugin_path))

                # Retain the prior version as a rollback restore point (replacing
                # any older one). On a fresh install there is no backup, so no
                # restore point is created. The ModuleManager health-gates the new
                # version and auto-reverts to this directory if verification fails.
                if backup_path.exists():
                    if previous_path.exists():
                        shutil.rmtree(previous_path, ignore_errors=True)
                    backup_path.rename(previous_path)

                return prior, self._read_manifest_version(plugin_path)

            prior_version, new_version = await asyncio.to_thread(_swap_in_new_version)
            self._record_history(
                None, repo, new_version,
                action="upgrade" if upgrade else "install",
                outcome="ok", previous_version=prior_version,
            )

            # 6. INTEGRATION: Announce the change via the event bus.
            # The ModuleManager will be listening for this.
            bus.emit("plugin:files_changed", {"action": "install", "name": safe_repo_name})
            log.info(f"SUCCESS: Plugin files for '{repo}' are in place. Notifying system.")
            bus.emit("plugin:installed", {"repo": repo, "path": str(plugin_path)})
            return True

        except Exception as e:
            log.error(f"INSTALL_ERROR: Installation failed for {repo}: {str(e)}", exc_info=True)
            
            # Revert from backup if swap failed midway
            backup_path = self.plugin_dir / f".backup_{safe_repo_name}"
            if backup_path.exists() and not plugin_path.exists():
                backup_path.rename(plugin_path)
                
            if plugin_path.exists() and not upgrade:
                shutil.rmtree(plugin_path, ignore_errors=True)
            bus.emit("plugin:install_failed", {"repo": repo, "error": str(e)})
            return False
        finally:
            if staging_base.exists():
                shutil.rmtree(staging_base, ignore_errors=True)

    async def _install_requirements(self, plugin_path: Path, timeout: int = 300):
        """Installs plugin vendor dependencies with a timeout."""
        req_file = plugin_path / "requirements.txt"
        if not req_file.exists():
            return

        # Validate requirements file doesn't contain suspicious entries. This is a
        # best-effort guard; full supply-chain safety (hash-pinning + sandboxing)
        # is tracked below.
        req_content = req_file.read_text()
        for line in req_content.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                continue
            lowered = stripped.lower()
            # Block local paths, parent-dir refs, direct VCS/URL installs and
            # PEP 508 "pkg @ <url>" specs, plus pip option lines (-e/--*) — all of
            # which can pull code from outside trusted package indexes.
            if (
                stripped.startswith('/')
                or stripped.startswith('..')
                or stripped.startswith('-')
                or '@' in stripped
                or '://' in lowered
                or lowered.startswith(('git+', 'http:', 'https:', 'file:'))
            ):
                log.error(f"SECURITY: Blocked suspicious requirement entry: {stripped}")
                return

        # TODO(agent): require hash-pinned requirements and isolate dependency
        # installs in a sandboxed runtime (CORE-AUTH-PLUGINS-008). Deferred —
        # needs a sandbox/runtime design beyond this pass.

        log.info(f"DEPENDENCIES: Installing requirements for {plugin_path.name} into private vendor directory...")
        vendor_dir = plugin_path / "vendor"
        vendor_dir.mkdir(exist_ok=True)

        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install",
                "--target", str(vendor_dir),
                "--no-input",
                "-r", str(req_file),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=timeout
            )
            if process.returncode != 0:
                log.error(f"PIP_ERROR: Dependency installation failed: {stderr.decode()[:500]}")
                shutil.rmtree(vendor_dir, ignore_errors=True)
            else:
                log.info("SUCCESS: All dependencies resolved into private vendor directory.")
        except asyncio.TimeoutError:
            log.error(f"TIMEOUT: pip install for {plugin_path.name} exceeded {timeout}s. Killing process.")
            process.kill()
            shutil.rmtree(vendor_dir, ignore_errors=True)

    async def uninstall_plugin(self, module_id: str, repo_name: str):
        """Löscht den Plugin-Ordner physisch."""
        plugin_path = self.plugin_dir / repo_name
        if not plugin_path.exists():
            log.warning(f"UNINSTALL: Plugin path {plugin_path} not found.")
            return False
        
        try:
            shutil.rmtree(plugin_path)
            # Also drop any retained restore points / stale backups for this plugin.
            safe_repo_name = repo_name.replace("-", "_")
            for stale in (
                self._previous_path(safe_repo_name),
                self.plugin_dir / f".backup_{safe_repo_name}",
                self.plugin_dir / f".failed_{safe_repo_name}",
            ):
                if stale.exists():
                    shutil.rmtree(stale, ignore_errors=True)
            # Announce the successful deletion so the ModuleManager can unload it from memory.
            bus.emit("plugin:files_changed", {"action": "uninstall", "id": module_id})
            log.info(f"SUCCESS: Plugin files for '{repo_name}' removed.")
            return True
        except Exception as e:
            log.error(f"ERROR: Failed to delete plugin files: {e}", exc_info=True)
            return False

    async def fetch_marketplace_data(self, force_refresh: bool = False):
        """Load plugin list from the local git clone (preferred) or fall back to HTTP.
        The local clone is kept up to date by the collection watcher / git-manager.
        """
        if not force_refresh and self._marketplace_cache and (time.time() - self._cache_timestamp < self._cache_ttl):
            log.debug("MARKETPLACE: Loading from cache")
            return [dict(p) for p in self._marketplace_cache]

        # Negative cache: a recent fetch failed and nothing has invalidated it
        # (git sync / force refresh) — answer from whatever we have with zero I/O.
        if not force_refresh and time.time() < self._negative_cache_until:
            log.debug("MARKETPLACE: Negative cache active, skipping remote fetch")
            if self._marketplace_cache:
                return [dict(p) for p in self._marketplace_cache]
            return [dict(p) for p in self._fetch_custom_marketplace_entries()]

        data = None

        # 1. Prefer the local git clone — fast, no network, no tokens.
        if COLLECTION_JSON_PATH.exists():
            try:
                data = json.loads(COLLECTION_JSON_PATH.read_text(encoding="utf-8"))
                log.debug(f"MARKETPLACE: Loaded collection from local clone ({COLLECTION_JSON_PATH})")
            except Exception as exc:
                log.warning(f"MARKETPLACE: Failed to read local collection JSON: {exc}")

        # 2. Fall back to HTTP if the clone isn't available yet.
        if data is None:
            log.info(f"MARKETPLACE: Local clone not ready, falling back to HTTP ({COLLECTION_FALLBACK_URL})")
            try:
                # Public raw.githubusercontent.com URL — no auth headers needed.
                async with httpx.AsyncClient(headers=self._get_headers(include_auth=False), follow_redirects=True) as client:
                    resp = await client.get(COLLECTION_FALLBACK_URL, timeout=5.0)
                    resp.raise_for_status()
                    data = resp.json()
            except Exception as exc:
                log.warning(f"MARKETPLACE: HTTP fallback also failed: {exc}")
                # Arm the negative cache so the next page opens don't pay the
                # network timeout again; a git sync or force refresh clears it.
                self._negative_cache_until = time.time() + 60.0
                # Collection unavailable — still surface custom repos. Prefer a
                # previously cached collection list if we have one.
                if self._marketplace_cache:
                    return [dict(p) for p in self._marketplace_cache]
                custom = self._fetch_custom_marketplace_entries()
                return [dict(p) for p in custom]

        raw_plugins = (data or {}).get("plugins", [])
        plugins = [self._collection_entry_to_marketplace(entry) for entry in raw_plugins]

        # Cache the collection's version tags by normalized URL so version
        # pickers can be populated without contacting GitHub.
        for entry in raw_plugins:
            versions = self._versions_with_latest(entry.get("version_tags"))
            for url in (entry.get("clone_url"), entry.get("html_url")):
                key = self._norm_url_key(url)
                if key:
                    self._collection_versions[key] = versions

        # Merge user-added custom repositories. Collection entries win on
        # repo_safe collisions so a curated plugin is never shadowed.
        custom = self._fetch_custom_marketplace_entries()
        if custom:
            seen = {p["repo_safe"] for p in plugins}
            for entry in custom:
                if entry["repo_safe"] not in seen:
                    plugins.append(entry)
                    seen.add(entry["repo_safe"])

        if plugins:
            self._marketplace_cache = plugins
            self._cache_timestamp = time.time()
            log.info(
                f"MARKETPLACE: Loaded {len(plugins)} plugin(s) "
                f"({len(custom)} custom)."
            )

        return [dict(p) for p in plugins]

    def _fetch_custom_marketplace_entries(self) -> list:
        """Marketplace entries for enabled custom repositories (DB-backed)."""
        try:
            from .custom_repo_service import custom_repo_service
            return [
                self._custom_repo_to_marketplace(row)
                for row in custom_repo_service.list_enabled()
            ]
        except Exception as exc:
            log.debug(f"MARKETPLACE: custom repo fetch skipped: {exc}")
            return []

plugin_service = PluginService()