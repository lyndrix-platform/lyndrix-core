"""
Core Git Service

Subscribes to the internal event bus and handles git:sync and
git:commit_push events.  All blocking git operations (clone, pull,
commit, push) run in a thread-pool executor so they never block the
asyncio event loop.

Bus events consumed:
  git:sync          — clone or pull a repository
  git:commit_push   — stage, commit and (optionally) push changes

Bus events emitted:
  git:status_update — result of every sync / commit-push operation

Payload for git:sync
  repo_id     str   unique identifier; used as the local directory name
  url         str   remote URL (https or ssh); omit for local-only repos
  auth_type   str   "none" | "token" | "ssh"  (default: "none")
  secret_value str  access-token or SSH private-key (PEM) string

Payload for git:commit_push
  repo_id     str
  message     str   commit message (default: "Update via Lyndrix")
  is_local    bool  if True, skip push even when a remote is configured
"""

import asyncio
import base64
import os
import re
import stat
import shutil
import tempfile
import subprocess
from collections import defaultdict
from pathlib import Path

from core.bus import bus
from core.logger import get_logger

log = get_logger("Core:Git")

# Repos are stored under /data/storage/git_repos  inside the container,
# but can be overridden via the GIT_STORAGE_DIR environment variable.
_DEFAULT_BASE = Path(os.environ.get("GIT_STORAGE_DIR", "/data/storage/git_repos"))

# repo_id is used as a directory name; restrict it to a safe charset so a crafted
# value (e.g. "../../etc") cannot escape the storage root.
_REPO_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _validate_repo_id(repo_id: str) -> None:
    """Reject repo_id values that could be used for path traversal."""
    if not repo_id or repo_id in {".", ".."} or not _REPO_ID_RE.match(repo_id):
        raise ValueError(f"Invalid repo_id: {repo_id!r}")


def _lazy_git():
    """Import gitpython lazily so the app starts even if it is not installed."""
    try:
        from git import Repo, Actor  # type: ignore[import]
        return Repo, Actor
    except ImportError as exc:
        raise RuntimeError(
            "gitpython is not installed. "
            "Add 'gitpython' to requirements.txt and rebuild the image."
        ) from exc


# ---------------------------------------------------------------------------
# Blocking helpers (all called via asyncio.to_thread / run_in_executor)
# ---------------------------------------------------------------------------

def _ensure_base(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def _ensure_safe_directory(path: Path, repo_id: str) -> None:
    """
    Mark *path* as a trusted git safe.directory.

    Docker-mounted volumes can have host ownership that differs from the
    container runtime user, which causes git to reject repo access with
    "detected dubious ownership". Registering the path avoids that failure.
    """
    repo_path = str(path.resolve())
    try:
        current = subprocess.run(
            ["git", "config", "--global", "--get-all", "safe.directory"],
            capture_output=True,
            text=True,
            check=False,
        )
        existing = {line.strip() for line in (current.stdout or "").splitlines() if line.strip()}
        if repo_path in existing:
            return

        subprocess.run(
            ["git", "config", "--global", "--add", "safe.directory", repo_path],
            check=True,
            capture_output=True,
            text=True,
        )
        log.info(f"[GIT:{repo_id}] Registered safe.directory: {repo_path}")
    except Exception as exc:
        log.warning(
            f"[GIT:{repo_id}] Could not register safe.directory for {repo_path}: {exc}"
        )


def _init_local(path: Path, repo_id: str) -> None:
    Repo, _ = _lazy_git()
    git_dir = path / ".git"
    if not git_dir.exists():
        log.info(f"[GIT:{repo_id}] Initializing new local repository at {path}")
        Repo.init(path)
        log.info(f"[GIT:{repo_id}] Local repository initialized.")
    else:
        log.debug(f"[GIT:{repo_id}] Local repository already exists at {path}.")


def _https_credential_env(token: str | None) -> dict:
    """
    Build a process environment that supplies the access token via an HTTP auth
    header instead of embedding it in the remote URL.

    Using GIT_CONFIG_* keeps the credential in-process only — it is never written
    to .git/config, so the token is not persisted at rest.
    """
    env = os.environ.copy()
    if not token:
        return env
    # Basic auth header equivalent to the old "oauth2:<token>@" URL form.
    auth = base64.b64encode(f"oauth2:{token}".encode()).decode()
    env["GIT_CONFIG_COUNT"] = "1"
    env["GIT_CONFIG_KEY_0"] = "http.extraHeader"
    env["GIT_CONFIG_VALUE_0"] = f"Authorization: Basic {auth}"
    return env


def _sync_https(url: str, token: str | None, path: Path, repo_id: str) -> None:
    Repo, _ = _lazy_git()
    # Credentials are passed per-operation via an HTTP header (see _https_credential_env);
    # the clean URL (no token) is what gets stored as the origin remote.
    env = _https_credential_env(token)

    def _force_sync_existing_repo() -> None:
        repo = Repo(str(path))
        repo.git.update_environment(**env)
        log.info(f"[GIT:{repo_id}] Fetching latest HTTPS changes…")
        repo.git.fetch("--prune", "origin")

        # In automation we prefer deterministic remote state over local history merges.
        branch_name = "main"
        try:
            branch_name = repo.active_branch.name
        except Exception:
            try:
                origin_head = repo.git.symbolic_ref("refs/remotes/origin/HEAD")
                branch_name = origin_head.rsplit("/", 1)[-1]
            except Exception:
                branch_name = "main"

        target_ref = f"origin/{branch_name}"
        repo.git.reset("--hard", target_ref)
        log.info(f"[GIT:{repo_id}] HTTPS sync completed (reset to {target_ref}).")

    for attempt in range(2):
        has_git_dir = (path / ".git").exists()
        try:
            if has_git_dir:
                _force_sync_existing_repo()
                return

            if any(path.iterdir()):
                log.warning(
                    f"[GIT:{repo_id}] Repo directory exists without .git metadata. "
                    "Resetting directory before clone."
                )
                shutil.rmtree(path, ignore_errors=True)
                path.mkdir(parents=True, exist_ok=True)

            log.info(f"[GIT:{repo_id}] Cloning HTTPS repository from {url}…")
            Repo.clone_from(url, str(path), env=env)
            log.info(f"[GIT:{repo_id}] HTTPS clone completed.")
            return
        except Exception as exc:
            err = str(exc)

            # Another worker may have completed the clone while this worker was running.
            if (path / ".git").exists():
                _force_sync_existing_repo()
                return

            race_detected = (
                "already exists and is not an empty directory" in err
                or "cannot copy" in err
            )
            if race_detected and attempt == 0:
                log.warning(f"[GIT:{repo_id}] Clone race detected. Retrying with clean directory.")
                shutil.rmtree(path, ignore_errors=True)
                path.mkdir(parents=True, exist_ok=True)
                continue

            raise


def _sync_ssh(url: str, private_key: str, path: Path, repo_id: str) -> None:
    """Clone or pull via SSH using a temporary key file (mode 0600, cleaned up after use)."""
    Repo, _ = _lazy_git()
    # Write the key to a temp file that is immediately deleted on close.
    fd, key_path = tempfile.mkstemp(prefix="lyndrix_git_", suffix=".pem")
    try:
        os.write(fd, private_key.encode())
        os.close(fd)
        os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)  # 0o600

        env = os.environ.copy()
        # Use a managed known_hosts file with StrictHostKeyChecking=accept-new:
        # the first connection to a host is trusted and pinned, but any later key
        # change is rejected (blocks MITM after first use). Override the file
        # location via GIT_KNOWN_HOSTS_FILE to pre-provision trusted host keys.
        known_hosts = os.environ.get(
            "GIT_KNOWN_HOSTS_FILE", str(_DEFAULT_BASE / ".ssh" / "known_hosts")
        )
        os.makedirs(os.path.dirname(known_hosts), exist_ok=True)
        env["GIT_SSH_COMMAND"] = (
            f"ssh -i {key_path} -o StrictHostKeyChecking=accept-new "
            f"-o UserKnownHostsFile={known_hosts} -o BatchMode=yes"
        )

        if not (path / ".git").exists():
            log.info(f"[GIT:{repo_id}] Cloning SSH repository from {url}…")
            Repo.clone_from(url, str(path), env=env)
            log.info(f"[GIT:{repo_id}] SSH clone completed.")
        else:
            log.info(f"[GIT:{repo_id}] Pulling latest SSH changes…")
            Repo(str(path)).remotes.origin.pull(env=env)
            log.info(f"[GIT:{repo_id}] SSH pull completed.")
    finally:
        try:
            os.unlink(key_path)
        except OSError:
            log.warning(f"[GIT:{repo_id}] Could not delete temporary SSH key file {key_path}.")


def _commit_and_push(
    repo_id: str,
    message: str,
    path: Path,
    is_local: bool,
    auth_type: str = "none",
    secret_value: str | None = None,
) -> str:
    """Stage all changes, commit and optionally push.  Returns a status string."""
    Repo, Actor = _lazy_git()
    index_lock = path / ".git" / "index.lock"
    if index_lock.exists():
        log.warning(f"[GIT:{repo_id}] Removing stale git index lock: {index_lock}")
        index_lock.unlink(missing_ok=True)

    repo = Repo(str(path))
    repo.git.add(A=True)

    if not repo.is_dirty(untracked_files=True):
        log.info(f"[GIT:{repo_id}] Working tree is clean — nothing to commit.")
        return "no_changes"

    author = Actor("Lyndrix IaC", "iac@lyndrix.local")
    repo.index.commit(message, author=author, committer=author)
    log.info(f"[GIT:{repo_id}] Committed: '{message}'")

    if not is_local and repo.remotes:
        if auth_type == "token" and secret_value:
            env = _https_credential_env(secret_value)
            repo.git.update_environment(**env)
        elif auth_type == "ssh" and secret_value:
            fd, key_path = tempfile.mkstemp(prefix="lyndrix_git_push_", suffix=".pem")
            try:
                os.write(fd, secret_value.encode())
                os.close(fd)
                os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
                known_hosts = os.environ.get(
                    "GIT_KNOWN_HOSTS_FILE", str(_DEFAULT_BASE / ".ssh" / "known_hosts")
                )
                ssh_cmd = (
                    f"ssh -i {key_path} -o StrictHostKeyChecking=accept-new "
                    f"-o UserKnownHostsFile={known_hosts} -o BatchMode=yes"
                )
                repo.git.update_environment(GIT_SSH_COMMAND=ssh_cmd)
                repo.remotes.origin.push()
            finally:
                try:
                    os.unlink(key_path)
                except OSError:
                    log.warning(f"[GIT:{repo_id}] Could not delete temporary SSH key file {key_path}.")
            log.info(f"[GIT:{repo_id}] Pushed to remote (SSH).")
            return "pushed"
        repo.remotes.origin.push()
        log.info(f"[GIT:{repo_id}] Pushed to remote.")
        return "pushed"

    return "committed_locally"


# ---------------------------------------------------------------------------
# GitService — subscribes to bus events
# ---------------------------------------------------------------------------

class GitService:
    def __init__(self, base_dir: Path = _DEFAULT_BASE) -> None:
        self.base_dir = base_dir
        self._repo_locks = defaultdict(asyncio.Lock)
        _ensure_base(self.base_dir)
        log.debug(f"[GIT:INIT] Base storage: {self.base_dir}")

        bus.subscribe("git:sync")(self.handle_sync)
        bus.subscribe("git:commit_push")(self.handle_commit_push)
        log.info("[GIT:INIT] Git service registered on event bus.")

    def _repo_path(self, repo_id: str) -> Path:
        # Validate the charset, then confirm the resolved path stays within base_dir
        # before any mkdir/rmtree can touch it.
        _validate_repo_id(repo_id)
        path = (self.base_dir / repo_id).resolve()
        base = self.base_dir.resolve()
        if base != path and base not in path.parents:
            raise ValueError(f"repo_id escapes storage root: {repo_id!r}")
        return path

    def _repo_lock(self, repo_id: str) -> asyncio.Lock:
        return self._repo_locks[repo_id]

    async def handle_sync(self, payload: dict) -> None:
        repo_id: str = payload.get("repo_id", "")
        if not repo_id:
            log.warning("[GIT:SYNC] Received git:sync without repo_id — ignoring.")
            return

        url: str | None = payload.get("url")
        auth_type: str = payload.get("auth_type", "none")
        secret: str | None = payload.get("secret_value")
        request_id: str | None = payload.get("request_id")
        try:
            path = self._repo_path(repo_id)
        except ValueError as exc:
            log.warning(f"[GIT:SYNC] Rejected invalid repo_id '{repo_id}': {exc}")
            bus.emit("git:status_update", {
                "repo_id": repo_id, "status": "error",
                "request_id": request_id, "error": "invalid repo_id",
            })
            return
        path.mkdir(parents=True, exist_ok=True)
        _ensure_safe_directory(path, repo_id)

        log.info(f"[GIT:SYNC] Starting sync for '{repo_id}' (auth_type={auth_type})")

        async with self._repo_lock(repo_id):
            try:
                if not url:
                    await asyncio.to_thread(_init_local, path, repo_id)
                elif auth_type == "ssh":
                    if not secret:
                        raise ValueError("auth_type=ssh requires secret_value (private key)")
                    await asyncio.to_thread(_sync_ssh, url, secret, path, repo_id)
                else:
                    await asyncio.to_thread(_sync_https, url, secret, path, repo_id)

                log.info(f"[GIT:SYNC] '{repo_id}' completed successfully.")
                bus.emit("git:status_update", {"repo_id": repo_id, "status": "synced", "request_id": request_id})
            except Exception as exc:
                log.error(f"[GIT:SYNC] Error syncing '{repo_id}': {exc}", exc_info=True)
                bus.emit("git:status_update", {
                    "repo_id": repo_id,
                    "status": "error",
                    "request_id": request_id,
                    "error": str(exc),
                })

    async def handle_commit_push(self, payload: dict) -> None:
        repo_id: str = payload.get("repo_id", "")
        if not repo_id:
            log.warning("[GIT:COMMIT] Received git:commit_push without repo_id — ignoring.")
            return

        message: str = payload.get("message", "Update via Lyndrix IaC GUI")
        is_local: bool = payload.get("is_local", False)
        auth_type: str = payload.get("auth_type", "none")
        secret_value: str | None = payload.get("secret_value")
        request_id: str | None = payload.get("request_id")
        try:
            path = self._repo_path(repo_id)
        except ValueError as exc:
            log.warning(f"[GIT:COMMIT] Rejected invalid repo_id '{repo_id}': {exc}")
            bus.emit("git:status_update", {
                "repo_id": repo_id, "status": "error",
                "request_id": request_id, "error": "invalid repo_id",
            })
            return
        _ensure_safe_directory(path, repo_id)

        log.info(f"[GIT:COMMIT] Starting commit/push for '{repo_id}' (is_local={is_local}, auth={auth_type})")

        async with self._repo_lock(repo_id):
            try:
                result = await asyncio.to_thread(
                    _commit_and_push, repo_id, message, path, is_local, auth_type, secret_value
                )
                log.info(f"[GIT:COMMIT] '{repo_id}' finished with result: {result}")
                bus.emit("git:status_update", {"repo_id": repo_id, "status": result, "request_id": request_id})
            except Exception as exc:
                log.error(f"[GIT:COMMIT] Error committing '{repo_id}': {exc}", exc_info=True)
                bus.emit("git:status_update", {
                    "repo_id": repo_id,
                    "status": "error",
                    "request_id": request_id,
                    "error": str(exc),
                })


# Module-level singleton — subscribes to the bus on import.
git_service = GitService()
