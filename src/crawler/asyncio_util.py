"""Small asyncio helpers shared across worker.py and engine.py -- nothing
here is specific to either.
"""

import asyncio
from collections.abc import Awaitable, Callable


async def wait(event: asyncio.Event, timeout: float, sleep: Callable[[float], Awaitable[None]]) -> None:
    """Waits for `event`, or `timeout` seconds via `sleep`, whichever comes
    first. A cancellable, clock-injectable stand-in for
    `asyncio.wait_for(event.wait(), timeout)`, which is bound to the real
    event loop clock and so can't be sped up in a test -- see
    fetch/rate_limiter.py's `now`/`sleep` for the same reasoning.
    """
    event_wait = asyncio.ensure_future(event.wait())
    timer = asyncio.ensure_future(sleep(timeout))
    try:
        await asyncio.wait({event_wait, timer}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for future in (event_wait, timer):
            if not future.done():
                future.cancel()
