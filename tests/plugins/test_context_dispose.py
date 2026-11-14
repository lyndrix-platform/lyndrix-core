"""BG-19: what a module registered through its context does not outlive it.

- ``ctx._tasks`` forgets finished tasks (it used to grow by one entry per task
  for the whole lifetime of the plugin);
- re-registering a gateway adapter stops the previous retry worker;
- ``dispose()`` unregisters the module's gateway adapters and theme overrides.
"""
import asyncio

import core.services  # noqa: F401  (import order: services before api)
from core.components.messaging.logic.adapter import GatewayAdapter, GatewayCapability
from core.components.messaging.logic.gateway import MessagingGateway, messaging_gateway
from core.components.plugins.logic.context import ModuleContext
from core.components.plugins.logic.models import ModuleManifest, ModulePermissions
from core.theming import get_theme_engine

PID = "lyndrix.plugin.test_dispose"


def _ctx() -> ModuleContext:
    manifest = ModuleManifest(id=PID, name="Dispose Tester", version="0.0.1", type="PLUGIN",
                              permissions=ModulePermissions(emit=[], subscribe=[]))
    return ModuleContext(manifest)


class _Adapter(GatewayAdapter):
    provider_id = "bg19_probe"
    display_name = "BG-19 probe"
    capabilities = GatewayCapability.TEXT

    async def send(self, message):
        return None


def test_finished_tasks_are_pruned_and_running_ones_cancelled_on_dispose():
    ctx = _ctx()

    async def run():
        async def quick():
            return 1

        async def forever():
            await asyncio.sleep(30)

        for _ in range(50):
            ctx.create_task(quick())
        keeper = ctx.create_task(forever())
        await asyncio.sleep(0.01)
        assert ctx._tasks == [keeper]              # the 50 finished tasks are gone

        ctx.dispose()
        await asyncio.sleep(0.01)
        assert keeper.cancelled() and ctx._tasks == []

    asyncio.run(run())


def test_reregistering_an_adapter_stops_the_previous_retry_worker():
    async def run():
        gateway = MessagingGateway()
        gateway.register(_Adapter())
        first = gateway._retry_workers["bg19_probe"]
        first_task = first._task
        assert first.running

        gateway.register(_Adapter())               # plugin reload
        second = gateway._retry_workers["bg19_probe"]
        await asyncio.sleep(0.01)
        assert second is not first and second.running
        assert first_task.cancelled() and not first.running
        gateway.unregister("bg19_probe")

    asyncio.run(run())


def test_dispose_unregisters_the_modules_gateway_adapter():
    async def run():
        ctx = _ctx()
        adapter = _Adapter()
        ctx.register_gateway_adapter(adapter)
        worker = messaging_gateway._retry_workers["bg19_probe"]
        assert messaging_gateway.get_adapter("bg19_probe") is adapter

        ctx.dispose()
        await asyncio.sleep(0.01)
        assert messaging_gateway.get_adapter("bg19_probe") is None and not worker.running

    asyncio.run(run())


def test_dispose_leaves_an_adapter_another_module_registered_meanwhile():
    async def run():
        ctx = _ctx()
        ctx.register_gateway_adapter(_Adapter())
        newer = _Adapter()
        messaging_gateway.register(newer)          # same provider id, other owner
        try:
            ctx.dispose()
            assert messaging_gateway.get_adapter("bg19_probe") is newer
        finally:
            messaging_gateway.unregister("bg19_probe")

    asyncio.run(run())


def test_dispose_drops_theme_overrides():
    ctx = _ctx()
    engine = get_theme_engine()
    ctx.register_theme_overrides({"CARD_BASE": "bg19-marker"})
    assert engine.resolve_component_styles(plugin_id=PID)["CARD_BASE"] == "bg19-marker"

    ctx.dispose()
    assert engine.resolve_component_styles(plugin_id=PID).get("CARD_BASE") != "bg19-marker"
