import asyncio
from collections.abc import Awaitable, Callable

from splitledger.contracts.messages import Inbound


# in process background job queue for inbound messages
class JobQueue:
    # start with an empty queue
    def __init__(self) -> None:
        self._queue: asyncio.Queue[Inbound] = asyncio.Queue()

    # add one job
    async def enqueue(self, inbound: Inbound) -> None:
        await self._queue.put(inbound)

    # run every queued job through the handler
    async def process_all(self, handle: Callable[[Inbound], Awaitable[None]]) -> None:
        while not self._queue.empty():
            await handle(self._queue.get_nowait())
            self._queue.task_done()

    # how many jobs are waiting
    def size(self) -> int:
        return self._queue.qsize()
