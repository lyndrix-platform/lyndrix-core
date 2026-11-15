"""Persist guard for the cluster bridge (SC-06).

A bridged event is written to the shared ``cluster_events`` table and handed to
every instance. A payload that carries a secret — a field named like one, or a
value shaped like one — must not end up there: such an event stays on the
instance that emitted it (local subscribers still receive it) and a warning
names the topic and the field, never the value.

The decision is a pure function of the payload (``core.logger.find_sensitive``,
the matcher the log masker uses); this module only adds the rate-limited
warning so a chatty emitter cannot flood the log.
"""
from __future__ import annotations

import time

from core.logger import find_sensitive, get_logger

log = get_logger("Core:Cluster:Bridge")

WARN_INTERVAL_SECONDS = 300.0
_MAX_TRACKED = 256
# (topic, field) -> monotonic time of the last warning
_last_warned: dict[tuple[str, str], float] = {}
blocked_total = 0


def sensitive_field(payload: dict) -> str | None:
    """Key path of the first secret-looking entry in ``payload``, or ``None``."""
    return find_sensitive(payload)


def blocks(topic: str, payload: dict, *, now: float | None = None) -> bool:
    """True when the event must not be published to the cluster.

    Side effect: one warning per ``(topic, field)`` every five minutes.
    """
    global blocked_total
    field = sensitive_field(payload)
    if field is None:
        return False
    blocked_total += 1
    moment = time.monotonic() if now is None else now
    key = (topic, field)
    last = _last_warned.get(key)
    if last is None or moment - last >= WARN_INTERVAL_SECONDS:
        if len(_last_warned) >= _MAX_TRACKED:
            _last_warned.clear()
        _last_warned[key] = moment
        log.warning(
            "BRIDGE: event '%s' was NOT published to the cluster — payload field '%s' "
            "looks like a secret. It was delivered on this instance only; pass a "
            "reference instead of the value.", topic, field,
        )
    return True
