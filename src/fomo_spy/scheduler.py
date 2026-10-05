"""Endpoint-wide bounded requests, with exits ahead of background work."""

import asyncio
import itertools
import time


class EndpointBudget:
    def __init__(self):
        self.condition = asyncio.Condition()
        self.queue = []
        self.sequence = itertools.count()
        self.tokens = 2.0
        self.updated = time.monotonic()
        self.inflight = 0
        self.cooldown = 0.0
        self.rate = None

    async def acquire(self, priority, rate):
        ticket = (priority, next(self.sequence))
        async with self.condition:
            self.rate = min(self.rate or rate, rate)
            self.queue.append(ticket)
            try:
                while True:
                    at = time.monotonic()
                    self.tokens = min(2, self.tokens + (at - self.updated) * self.rate)
                    self.updated = at
                    delay = max(self.cooldown - at, (1 - self.tokens) / self.rate, 0)
                    if min(self.queue) == ticket and self.inflight < 2 and delay <= 0:
                        self.tokens -= 1
                        self.inflight += 1
                        return
                    try:
                        await asyncio.wait_for(
                            self.condition.wait(), max(0.01, delay) if delay else 0.1
                        )
                    except TimeoutError:
                        pass
            finally:
                self.queue.remove(ticket)
                self.condition.notify_all()

    async def release(self):
        async with self.condition:
            self.inflight -= 1
            self.condition.notify_all()
