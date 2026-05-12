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
from core.logger import get_logger
from core.bus import bus

log = get_logger("Core:PluginService")

# --- Plugin collection source config ---
# The collection is cloned locally by the git-manager plugin.
# git-manager stores repos under /data/storage/git_repos/{repo_id}.
COLLECTION_REPO_URL = "https://github.com/marvin1309/lyndrix-plugin-collection.git"
COLLECTION_REPO_ID = "lyndrix-plugin-collection"
COLLECTION_LOCAL_PATH = Path("/data/storage/git_repos") / COLLECTION_REPO_ID
COLLECTION_JSON_PATH = COLLECTION_LOCAL_PATH / "plugin-directory" / "plugins.json"

# HTTP fallback (used when the local clone is not yet available).
# Override via PLUGIN_COLLECTION_URL env var.
COLLECTION_FALLBACK_URL = os.environ.get(
    "PLUGIN_COLLECTION_URL",
    "https://raw.githubusercontent.com/marvin1309/lyndrix-plugin-collection/main/plugin-directory/plugins.json",
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
        self._cache_ttl = 900  # 15 Minuten Cache-Dauer
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

    def _normalize_repo_name(self, repo_name: str) -> str:
        return repo_name.replace("-", "_")

    def _repo_aliases(self, repo_name: str):
        normalized = self._normalize_repo_name(repo_name)
        aliases = {normalized}
        if normalized.startswith("lyndrix_"):
            aliases.add(normalized[len("lyndrix_"):])
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

    def _collection_entry_to_marketplace(self, entry: dict) -> dict:
        """Convert a plugins.json entry from lyndrix-plugin-collection to marketplace format."""
        html_url = entry.get("html_url", "")
        name = entry.get("name", "")
        full_name = entry.get("full_name", "")
        author = full_name.split("/")[0] if "/" in full_name else "Unknown"
        repo_safe = self._normalize_repo_name(name)
        return {
            "name": name.replace("-", " ").title(),
            "description": entry.get("description") or "No description available.",
            "stars": entry.get("stargazers_count", 0),
            "url": html_url,
            "clone_url": entry.get("clone_url", html_url),
            "author": author,
            "repo_safe": repo_safe,
            "repo_aliases": sorted(self._repo_aliases(name)),
            "tags": ["latest"],
            "topics": entry.get("topics", []),
            "archived": entry.get("archived", False),
            "metadata_source": "collection",
        }

    def _get_headers(self, include_auth: bool = True) -> dict:
        """Build request headers. Set include_auth=False for public CDN URLs."""
        headers = {
            "User-Agent": "Lyndrix-Core/1.0",
            "Accept": "application/vnd.github.v3+json"
        }
        if not include_auth:
            return headers
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            try:
                from core.services import vault_instance
                if vault_instance.is_connected:
                    resp = vault_instance.client.secrets.kv.v2.read_secret_version(path="core/settings", mount_point="lyndrix")
                    token = resp['data']['data'].get('github_token')
            except Exception: pass
            
        if token:
            headers["Authorization"] = f"token {token}"
        return headers

    async def get_plugin_versions(self, github_url: str, force_refresh: bool = False):
        if not force_refresh and github_url in self._tag_cache:
            if time.time() - self._tag_cache_timestamp.get(github_url, 0) < self._cache_ttl:
                return self._tag_cache[github_url]

        try:
            user, repo = self._extract_repo_info(github_url)
        except ValueError:
            return []
            
        api_url = f"{self.github_api_base}/{user}/{repo}/tags"
        try:
            async with httpx.AsyncClient(headers=self._get_headers(), follow_redirects=True) as client:
                resp = await client.get(api_url)
                if resp.status_code == 200:
                    tags = resp.json()
                    raw_tags = [t['name'] for t in tags]
                    def parse_v(t):
                        c = t.lstrip('v')
                        parts = []
                        for p in re.split(r'[^0-9]+', c):
                            if p: parts.append(int(p))
                        return parts
                    tag_list = sorted(raw_tags, key=parse_v, reverse=True)
                    self._tag_cache[github_url] = tag_list
                    self._tag_cache_timestamp[github_url] = time.time()
                    return tag_list
        except Exception as e:
            log.error(f"Failed to fetch tags for {github_url}: {e}")
        return []

    async def install_plugin(self, github_url: str, version: str = "latest", upgrade: bool = False):
        """Downloads, extracts and registers a new plugin from GitHub."""
        user, repo = self._extract_repo_info(github_url)
        
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
            async with httpx.AsyncClient(follow_redirects=True, headers=self._get_headers()) as client:
                # 1. Fetch Repository Metadata
                api_url = f"{self.github_api_base}/{user}/{repo}"
                resp = await client.get(api_url)
                
                if resp.status_code == 403:
                    log.warning(f"INSTALL: Rate limit hit for metadata. Assuming 'main' branch.")
                    default_branch = "main"
                else:
                    resp.raise_for_status()
                    repo_info = resp.json()
                    default_branch = repo_info.get("default_branch", "main")
                
                # 2. Download Archive
                if version == "latest":
                    zip_url = f"https://github.com/{user}/{repo}/archive/refs/heads/{default_branch}.zip"
                else:
                    zip_url = f"https://github.com/{user}/{repo}/archive/refs/tags/{version}.zip"

                log.info(f"DOWNLOAD: Fetching source from {zip_url}")
                response = await client.get(zip_url)
                
                # Fallback falls der Branch falsch ist (z.B. master statt main)
                if response.status_code == 404 and version == "latest" and default_branch == "main":
                    log.info("DOWNLOAD: 'main' not found, trying 'master'...")
                    zip_url = f"https://github.com/{user}/{repo}/archive/refs/heads/master.zip"
                    response = await client.get(zip_url)
                
                response.raise_for_status()
                
                with open(zip_path, 'wb') as f:
                    f.write(response.content)

            # 3. Safe Extraction into Staging (ZIP Slip protected)
            log.info("FILESYSTEM: Extracting archive into hidden staging area...")
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                # TODO: require signed/allowlisted plugin sources before extraction and install.
                root_folder = zip_ref.namelist()[0].split('/')[0]
                # Validate all paths stay within staging_base
                for member in zip_ref.namelist():
                    member_path = (staging_base / member).resolve()
                    if not str(member_path).startswith(str(staging_base.resolve())):
                        raise ValueError(f"SECURITY: ZIP contains path traversal entry: {member}")
                zip_ref.extractall(staging_base)
                extracted_dir = staging_base / root_folder
            
            # 4. Dependency Management in Staging
            await self._install_requirements(extracted_dir)

            # 5. ATOMIC SWAP (Protects against dev server hot-reload crashes)
            backup_path = self.plugin_dir / f".backup_{safe_repo_name}"
            if plugin_path.exists():
                log.info(f"UPGRADE: Swapping old directory {plugin_path} for new version...")
                if backup_path.exists():
                    shutil.rmtree(backup_path, ignore_errors=True)
                plugin_path.rename(backup_path)
            
            shutil.move(str(extracted_dir), str(plugin_path))
            
            if backup_path.exists():
                shutil.rmtree(backup_path, ignore_errors=True)

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

        # Validate requirements file doesn't contain suspicious entries
        req_content = req_file.read_text()
        for line in req_content.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith('#'):
                # Block entries that look like local paths or URLs (potential injection)
                if stripped.startswith('/') or stripped.startswith('..'):
                    log.error(f"SECURITY: Blocked suspicious requirement entry: {stripped}")
                    return

        # TODO: require hash-pinned requirements and isolate dependency installs in a sandboxed runtime.

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
                    resp = await client.get(COLLECTION_FALLBACK_URL, timeout=10.0)
                    resp.raise_for_status()
                    data = resp.json()
            except Exception as exc:
                log.warning(f"MARKETPLACE: HTTP fallback also failed: {exc}")
                return [dict(p) for p in self._marketplace_cache]

        raw_plugins = data.get("plugins", [])
        plugins = [self._collection_entry_to_marketplace(entry) for entry in raw_plugins]

        if plugins:
            self._marketplace_cache = plugins
            self._cache_timestamp = time.time()
            log.info(f"MARKETPLACE: Loaded {len(plugins)} plugin(s) from collection index.")

        return [dict(p) for p in plugins]

plugin_service = PluginService()