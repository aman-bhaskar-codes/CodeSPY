"""Bounded dispatcher/consumer/recovery loops over explicitly injected ports."""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from app.ports.queue import Delivery
from app.workers.consumer import JobConsumer
from app.workers.dispatcher import OutboxDispatcher

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

        tasks = [
            asyncio.create_task(periodic(self.dispatcher.dispatch_once, 1)),
            asyncio.create_task(periodic(recovery, 15)),
            asyncio.create_task(periodic(cleanup, 15)),
            asyncio.create_task(consume()),
        ]
        try:
            await stop.wait()
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
