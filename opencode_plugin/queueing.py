"""Async queue using the pure pending-order rule. No model/transport dependencies."""

import asyncio
import time

from .logic import queue_execution_order


class RequestQueue:
    def __init__(self, maxsize, *, reorder_window=0.025):
        self.maxsize = maxsize
        self.reorder_window = reorder_window
        self.pending = []
        self.active = set()
        self.condition = asyncio.Condition()

    def qsize(self):
        return len(self.pending)

    async def put(self, job):
        async with self.condition:
            await self.condition.wait_for(lambda: len(self.pending) < self.maxsize)
            self.pending.append(job)
            self.condition.notify_all()

    async def discard(self, job):
        async with self.condition:
            if job in self.pending:
                self.pending.remove(job)
                self.condition.notify_all()

    async def get(self):
        async with self.condition:
            while True:
                records = [
                    {"id": j.request_id, "session": j.scope or j.request_id, "sequence": j.sequence}
                    for j in self.pending
                ]
                order = queue_execution_order(records, active_sessions=self.active)
                if not order:
                    await self.condition.wait()
                    continue
                selected = next(j for j in self.pending if j.request_id == order[0])
                remaining = selected.enqueued + self.reorder_window - time.monotonic()
                if remaining > 0:
                    try:
                        await asyncio.wait_for(self.condition.wait(), remaining)
                    except asyncio.TimeoutError:
                        pass
                    continue
                self.pending.remove(selected)
                self.active.add(selected.scope or selected.request_id)
                self.condition.notify_all()
                return selected

    async def task_done(self, job):
        async with self.condition:
            self.active.discard(job.scope or job.request_id)
            self.condition.notify_all()
