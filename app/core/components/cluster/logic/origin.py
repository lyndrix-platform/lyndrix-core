"""Relay origin — "this code runs because a peer's event was relayed here".

Set by the bus bridge while a relayed event is dispatched locally. Everything
a handler emits in reaction — synchronously, or from the task/thread the bus
spawns for it (``asyncio.create_task`` and ``asyncio.to_thread`` copy the
context) — is a *derived* event and must not be published again: four
instances each turning one relayed "plugin:installed" into a fresh
"notification:new" and bridging that produced 16 toasts per browser for one
install.

The suppression is meant for direct reactions. It must end where something
with a life of its own begins: a plugin that a peer-triggered reconcile
activates, a lease worker started from a relayed hand-over. Those inherit the
context too — and with it, forever, the mark "derived from a relayed event":
none of their later cluster events would ever be published. Lifecycle
boundaries therefore start their work in :func:`clean_context`.

Kept free of imports so the bus users at those boundaries (plugin context,
module manager, lease keeper) can import it without pulling in the bridge.
"""
from __future__ import annotations

import contextvars
from typing import Optional

RELAY_ORIGIN: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("lyndrix_relay_origin", default=None)


def clean_context() -> contextvars.Context:
    """A copy of the current context without the relay mark — for work that
    outlives the event that triggered it (``Context.run`` / ``create_task(context=…)``)."""
    context = contextvars.copy_context()
    context.run(RELAY_ORIGIN.set, None)
    return context
