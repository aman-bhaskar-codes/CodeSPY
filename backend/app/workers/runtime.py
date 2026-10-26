"""Bounded dispatcher/consumer/recovery loops over explicitly injected ports."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.ports.queue import Delivery
from app.workers.consumer import JobConsumer
from app.workers.dispatcher import OutboxDispatcher
from app.workers.memory_maintenance import MemoryMaintenanceWorker

logger = logging.getLogger(__name__)


class WorkerRuntime:
    def __init__(self, dispatcher: OutboxDispatcher, consumer: JobConsumer):
        self.dispatcher, self.consumer = dispatcher, consumer

    async def handle(self, delivery: Delivery) -> None:
        await self.consumer.handle(delivery)

    async def run(
        self,
        stop: asyncio.Event,
        recovery: Callable[[], Awaitable[int]],
        cleanup: Callable[[], Awaitable[int]],
        *,
        model_maintenance: Callable[[], Awaitable[int]] | None = None,
        memory_maintenance: Callable[[], Awaitable[int]] | None = None,
    ) -> None:
        async def periodic(action: Callable[[], Awaitable[int]], interval: float) -> None:
            while not stop.is_set():
                try:
                    await action()
                except Exception:
                    logger.warning("background_dependency_unavailable")
                try:
                    async with asyncio.timeout(interval):
                        await stop.wait()
                except TimeoutError:
                    pass

        async def consume() -> None:
            while not stop.is_set():
                try:
                    deliveries = await self.consumer.queue.receive(2, wait_seconds=20)
                    # At most two operations; DB advisory capacity also spans processes.
                    results = await asyncio.gather(
                        *(self.handle(delivery) for delivery in deliveries), return_exceptions=True
                    )
                    if any(isinstance(result, BaseException) for result in results):
                        logger.warning("delivery_dependency_unavailable")
                except Exception:
                    logger.warning("consumer_dependency_unavailable")
                    try:
                        async with asyncio.timeout(1):
                            await stop.wait()
                    except TimeoutError:
                        pass

        index_operation = getattr(self.consumer, "index_operation", None)
        if index_operation is not None:
            await index_operation.recover_owned()
        tasks = [
            asyncio.create_task(periodic(self.dispatcher.dispatch_once, 1)),
            asyncio.create_task(periodic(recovery, 15)),
            asyncio.create_task(periodic(cleanup, 15)),
            asyncio.create_task(consume()),
        ]
        investigation = getattr(self.consumer, "investigation_operation", None)
        proposals = getattr(investigation, "memory_proposals", None)
        if proposals is not None:
            tasks.append(asyncio.create_task(periodic(proposals.tick, 15)))
        if model_maintenance is not None:
            tasks.append(asyncio.create_task(periodic(model_maintenance, 15)))
        if (
            memory_maintenance is None
            and (factory := getattr(self.consumer, "factory", None)) is not None
        ):
            memory_maintenance = MemoryMaintenanceWorker(factory).tick
        if memory_maintenance is not None:
            tasks.append(asyncio.create_task(periodic(memory_maintenance, 60)))
        scan_operation = getattr(self.consumer, "scan_operation", None)
        if scan_operation is not None:
            tasks.append(
                asyncio.create_task(periodic(scan_operation.capabilities.recover_owned, 15))
            )
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
