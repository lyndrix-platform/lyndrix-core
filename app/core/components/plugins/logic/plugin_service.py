import os
import sys
import shutil
import asyncio
import zipfile
import time
import httpx
import re
from pathlib import Path
from core.logger import get_logger
from core.bus import bus

log = get_logger("Core:PluginService")

class PluginService:
    def __init__(self):
        # FIX: Robust path finding
        # app/core/components/plugins/logic/plugin_service.py -> app/plugins
        self.plugin_dir = Path(__file__).parents[4] / "plugins"
        self.plugin_dir.mkdir(parents=True, exist_ok=True)
        self.github_api_base = "https://api.github.com/repos"
        
        # Cache für Marketplace-Daten (URL -> Daten)
        self._marketplace_cache = []
        self._cache_timestamp = 0
        self._tag_cache = {}
        self._tag_cache_timestamp = {}
        self._cache_ttl = 900  # 15 Minuten Cache-Dauer

    def _extract_repo_info(self, github_url: str):
        parts = github_url.rstrip("/").split("/")
        if len(parts) >= 2:
            repo = parts[-1]
            if repo.endswith(".git"):
                repo = repo[:-4]
            return parts[-2], repo
        raise ValueError("Invalid GitHub URL format")

    def _get_headers(self):
        """Erstellt die Header für GitHub API Anfragen."""
        headers = {
            "User-Agent": "Lyndrix-Core/1.0",
            "Accept": "application/vnd.github.v3+json"
        }
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
        zip_path = self.plugin_dir / f"{repo}.zip"
        extracted_dir = None
        
        action_name = "UPGRADE" if upgrade else "INSTALL"
        log.info(f"{action_name}: Requesting plugin '{repo}' version '{version}' from {github_url}")
        bus.emit("plugin:install_started", {"repo": repo, "version": version})

        if plugin_path.exists():
            if upgrade:
                log.info(f"UPGRADE: Removing old directory {plugin_path}")
                shutil.rmtree(plugin_path)
            else:
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

            # 3. Extraction
            log.info("FILESYSTEM: Extracting archive and cleaning up")
            with zipfile.ZipFile(zip_path, 'r') as zip_ref:
                # GitHub-Archive haben immer einen Root-Ordner wie 'repo-branchname'
                root_folder = zip_ref.namelist()[0].split('/')[0]
                extracted_dir = self.plugin_dir / root_folder
                
                # Cleanup alter Extraktionen falls vorhanden
                if extracted_dir.exists():
                    shutil.rmtree(extracted_dir)
                    
                zip_ref.extractall(self.plugin_dir)
            
            extracted_dir.rename(plugin_path)

            # 4. Dependency Management
            await self._install_requirements(plugin_path)

            # 5. INTEGRATION: Announce the change via the event bus.
            # The ModuleManager will be listening for this.
            bus.emit("plugin:files_changed", {"action": "install", "name": safe_repo_name})
            log.info(f"SUCCESS: Plugin files for '{repo}' are in place. Notifying system.")
            bus.emit("plugin:installed", {"repo": repo, "path": str(plugin_path)})
            return True

        except Exception as e:
            log.error(f"INSTALL_ERROR: Installation failed for {repo}: {str(e)}", exc_info=True)
            if plugin_path.exists():
                shutil.rmtree(plugin_path)
            if extracted_dir and extracted_dir.exists():
                shutil.rmtree(extracted_dir)
            bus.emit("plugin:install_failed", {"repo": repo, "error": str(e)})
            return False
        finally:
            if zip_path.exists():
                zip_path.unlink()

    async def _install_requirements(self, plugin_path: Path):
        req_file = plugin_path / "requirements.txt"
        if req_file.exists():
            log.info(f"DEPENDENCIES: Installing requirements for {plugin_path.name} into private vendor directory...")
            vendor_dir = plugin_path / "vendor"
            vendor_dir.mkdir(exist_ok=True)

            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "pip", "install",
                "--target", str(vendor_dir),
                "-r", str(req_file),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            if process.returncode != 0:
                log.error(f"PIP_ERROR: Dependency installation failed: {stderr.decode()}")
                shutil.rmtree(vendor_dir, ignore_errors=True)
            else:
                log.info("SUCCESS: All dependencies resolved into private vendor directory.")

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
        """Liest die plugin-list.txt und holt Metadaten von GitHub."""
        # Cache-Check: Wenn Daten noch frisch sind, API-Anrufe sparen
        if not force_refresh and self._marketplace_cache and (time.time() - self._cache_timestamp < self._cache_ttl):
            log.debug("MARKETPLACE: Loading from cache")
            return self._marketplace_cache

        list_file = Path(__file__).parents[4] / "assets" / "plugin-list.txt"
        if not list_file.exists():
            return []

        plugins = []
        async with httpx.AsyncClient(headers=self._get_headers(), follow_redirects=True) as client:
            with open(list_file, "r") as f:
                urls = [line.strip() for line in f.readlines() if line.strip()]
            
            for url in urls:
                try:
                    user, repo = self._extract_repo_info(url)
                    api_url = f"{self.github_api_base}/{user}/{repo}"
                    resp = await client.get(api_url)
                    if resp.status_code == 200:
                        data = resp.json()
                        plugins.append({
                            "name": data.get("name"),
                            "description": data.get("description", "Keine Beschreibung verfügbar."),
                            "stars": data.get("stargazers_count", 0),
                            "url": data.get("html_url"),
                            "clone_url": url, # Für den Installer
                            "author": data.get("owner", {}).get("login", "Unknown")
                        })
                    elif resp.status_code == 403:
                        log.warning(f"MARKETPLACE: Rate limit hit for {repo}. Using fallback data.")
                        plugins.append({
                            "name": repo,
                            "description": "Metadaten konnten nicht geladen werden (GitHub Rate Limit).",
                            "stars": "N/A",
                            "url": url,
                            "clone_url": url,
                            "author": user
                        })
                except Exception as e:
                    log.warning(f"MARKETPLACE: Failed to fetch info for {url}: {e}")
        
        # Cache aktualisieren
        if plugins:
            self._marketplace_cache = plugins
            self._cache_timestamp = time.time()
            
        return plugins

plugin_service = PluginService()