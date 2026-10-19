import asyncio

from app.ports.queue import Delivery
from app.workers.runtime import WorkerRuntime


async def test_shutdown_owns_sibling_when_another_delivery_fails():
    started, finished, stop = asyncio.Event(), asyncio.Event(), asyncio.Event()
    owned = []

    class Queue:
        first = True

        async def receive(self, *_args, **_kwargs):
            if self.first:
                self.first = False
                return [Delivery(body=b"bad", receipt="one"), Delivery(body=b"long", receipt="two")]
            await asyncio.Event().wait()

    class Consumer:
        queue = Queue()

        async def handle(self, delivery):
            if delivery.body == b"bad":
                raise RuntimeError("fixture failure")
            owned.append(asyncio.current_task())
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.set()

    class Dispatcher:
        async def dispatch_once(self):
            return 0

    async def noop():
        return 0

    running = asyncio.create_task(WorkerRuntime(Dispatcher(), Consumer()).run(stop, noop, noop))
    try:
        async with asyncio.timeout(1):
            await started.wait()
            for _ in range(3):
                await asyncio.sleep(0)
            stop.set()
            await running
        assert finished.is_set()
    finally:
        running.cancel()
        for task in owned:
            task.cancel()
        await asyncio.gather(running, *owned, return_exceptions=True)
