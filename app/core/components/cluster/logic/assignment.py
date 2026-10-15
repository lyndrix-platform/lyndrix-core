"""Work assignment: which instances run a named workload, and which share of it.

A plugin with work that can be spread over several instances (probing 200
hosts, polling 40 Docker hosts, syncing N repos) asks the cluster instead of
inventing its own election:

    workers = ctx.cluster_workers("monitoring:probe", count=2, roles=["runner"])
    mine    = ctx.my_shard("monitoring:probe", monitor_ids, count=2, roles=["runner"])

Both answers are computed **locally on every instance from the same inputs** —
the live instance list the heartbeats maintain — with rendezvous hashing, so all
instances agree without a round trip, and a membership change moves only the
keys of the instance that left or joined (no global reshuffle). Plugins
recompute on ``cluster:changed`` (and may poll); the result is at-least-once
during the few seconds in which heartbeat views differ. For exactly-once
singleton work use a lease (``ctx.on_lease``) instead.

Rules: ``count`` ≤ live candidates (fewer live → fewer workers, never none while
one candidate lives); ``roles`` filters candidates (``None`` = every live
instance); draining instances are excluded; single mode returns this instance.
"""
from __future__ import annotations

import hashlib
import time
from typing import Iterable, Optional


def _score(*parts: str) -> int:
    return int.from_bytes(hashlib.blake2b("|".join(parts).encode(), digest_size=8).digest(), "big")


class WorkAssigner:
    """Pure functions over the live instance list + a registry of the last answers
    (for the Cluster page / ``/api/cluster``)."""

    def __init__(self, service):
        self._service = service
        # name → {"count", "roles", "workers", "keys", "mine", "at"}
        self.assignments: dict[str, dict] = {}

    # ----------------------------------------------------------------- inputs
    def candidates(self, roles: Optional[Iterable[str]] = None) -> list[str]:
        """Live, non-draining instance ids (optionally having one of ``roles``)."""
        wanted = {r for r in (roles or []) if r}
        out: list[str] = []
        instances = list(getattr(self._service, "_instances", None) or [])
        if not instances:  # single mode / coordinator not up yet: this instance
            me = self._service.instance_id
            if not wanted or wanted & set(self._service.roles):
                out.append(me)
            return out
        for inst in instances:
            if not getattr(inst, "alive", False):
                continue
            meta = getattr(inst, "meta", None) or {}
            if meta.get("draining"):
                continue
            if wanted and not (wanted & set(getattr(inst, "roles", None) or [])):
                continue
            out.append(inst.instance_id)
        return sorted(set(out))

    # ---------------------------------------------------------------- answers
    def workers(self, name: str, count: int = 1, roles: Optional[Iterable[str]] = None) -> list[str]:
        """The ``count`` instances that run workload ``name`` (stable order, rendezvous)."""
        cands = self.candidates(roles)
        n = max(1, int(count or 1))
        chosen = sorted(cands, key=lambda iid: _score(name, iid), reverse=True)[:n]
        self._record(name, count=n, roles=roles, workers=chosen)
        return chosen

    def is_worker(self, name: str, count: int = 1, roles: Optional[Iterable[str]] = None) -> bool:
        return self._service.instance_id in self.workers(name, count, roles)

    def shard(self, name: str, keys: Iterable, count: Optional[int] = None,
              roles: Optional[Iterable[str]] = None) -> dict[str, list]:
        """Distribute ``keys`` over the workers of ``name`` → ``{instance_id: [keys]}``.

        ``count=None`` spreads over every candidate. Each key goes to the worker with
        the highest rendezvous score for that key, so adding/removing a worker moves
        only that worker's keys.
        """
        keys = list(keys)
        cands = self.candidates(roles)
        n = len(cands) if count is None else max(1, int(count))
        chosen = sorted(cands, key=lambda iid: _score(name, iid), reverse=True)[:n]
        out: dict[str, list] = {iid: [] for iid in chosen}
        if chosen:
            for key in keys:
                owner = max(chosen, key=lambda iid: _score(name, str(key), iid))
                out[owner].append(key)
        me = self._service.instance_id
        self._record(name, count=n, roles=roles, workers=chosen, keys=len(keys), mine=len(out.get(me, [])))
        return out

    def my_shard(self, name: str, keys: Iterable, count: Optional[int] = None,
                 roles: Optional[Iterable[str]] = None) -> list:
        return self.shard(name, keys, count, roles).get(self._service.instance_id, [])

    # ------------------------------------------------------------- reporting
    def _record(self, name: str, *, count: int, roles, workers: list[str], keys: int | None = None,
                mine: int | None = None) -> None:
        entry = self.assignments.setdefault(name, {})
        entry.update({
            "count": count,
            "roles": sorted({r for r in (roles or []) if r}) or None,
            "workers": list(workers),
            "at": time.time(),
        })
        if keys is not None:
            entry["keys"] = keys
            entry["mine"] = mine
        # bounded registry: forget workloads nobody asked about for an hour
        stale = [n for n, e in self.assignments.items() if time.time() - e.get("at", 0) > 3600]
        for n in stale:
            self.assignments.pop(n, None)

    def report(self) -> list[dict]:
        me = self._service.instance_id
        return [
            {"name": name, **entry, "is_worker": me in entry.get("workers", [])}
            for name, entry in sorted(self.assignments.items())
        ]
