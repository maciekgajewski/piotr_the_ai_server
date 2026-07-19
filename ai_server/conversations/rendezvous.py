from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
from typing import Generic, TypeVar


T = TypeVar("T")


class RendezvousClosed(Exception):
    pass


@dataclass
class _Offer(Generic[T]):
    item: T
    accepted: asyncio.Future[None]


class Rendezvous(Generic[T]):
    """A cancellation-safe zero-capacity asynchronous handoff."""

    def __init__(self) -> None:
        self._condition = asyncio.Condition()
        self._offers: deque[_Offer[T]] = deque()
        self._closed: BaseException | None = None

    async def send(self, item: T) -> None:
        offer = _Offer(item=item, accepted=asyncio.get_running_loop().create_future())
        async with self._condition:
            if self._closed is not None:
                raise self._closed
            self._offers.append(offer)
            self._condition.notify_all()

        try:
            await asyncio.shield(offer.accepted)
        except asyncio.CancelledError:
            async with self._condition:
                if offer in self._offers:
                    self._offers.remove(offer)
                    offer.accepted.cancel()
                    raise
            # The receiver already removed the offer.  Delivery is committed,
            # so cancellation cannot make the sender report that it vanished.
            await asyncio.shield(offer.accepted)

    async def receive(self) -> T:
        async with self._condition:
            while not self._offers:
                if self._closed is not None:
                    raise self._closed
                await self._condition.wait()
            offer = self._offers.popleft()
            offer.accepted.set_result(None)
            return offer.item

    async def close(self, exception: BaseException | None = None) -> None:
        failure = exception or RendezvousClosed()
        async with self._condition:
            if self._closed is not None:
                return
            self._closed = failure
            for offer in self._offers:
                if not offer.accepted.done():
                    offer.accepted.set_exception(failure)
            self._offers.clear()
            self._condition.notify_all()
