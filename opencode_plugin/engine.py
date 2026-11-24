"""Shared bounded queue, persistent ACP runtime, and authoritative history reconciliation."""

import asyncio
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from .acp import ACPClient
from .errors import ImageLimitError, OpenCodeError
from .images import prompt_images
from .launcher import ProcessOwner
from .logic import history_state, reconcile_history
from .metrics import record
from .protocol import encode
from .queueing import RequestQueue
from .token_usage import TOKEN_FIELDS, TurnTokens, previous_user, turn_tokens
from .transcript import render_messages


@dataclass
class Job:
    messages: list
    scope: str | None
    request_id: str
    future: asyncio.Future
    enqueued: float
    deadline: float
    input_bytes: int
    started: asyncio.Event = field(default_factory=asyncio.Event)
    cancel: asyncio.Event = field(default_factory=asyncio.Event)
    sent_bytes: int = 0
    wire_bytes: int = 0
    reused: bool = False
    reset_reason: str = "new_conversation"
    output_bytes: int = 0
    queue_ms: int | None = None
    process_reused: bool = False
    accounted: bool = False
    sequence: int | None = None
    stats_session: str | None = None
    model_id: str | None = None
    tokens: TurnTokens | None = None
    images: dict | None = None
    on_chunk: Callable[[str], None] | None = None
    sent_image_blocks: int = 0
    sent_image_base64_bytes: int = 0
    historical_images_not_attached: int = 0
    error_metadata: dict = field(default_factory=dict)


class Engine:
    def __init__(self, cfg, *, owner_factory=ProcessOwner):
        self.cfg = cfg
        self.owner_factory = owner_factory
        self.references = 0
        self.queue = RequestQueue(cfg["queue_capacity"])
        self.workers = []
        self.sessions = {}
        self.client = None
        self.owner = None
        self.active = 0
        self.session_slots = 0
        self.closed = False
        self._condition = asyncio.Condition()
        self._start_lock = asyncio.Lock()
        self._jobs = {}
        self.last_failure = None

    async def _start_workers(self):
        async with self._start_lock:
            if self.closed:
                raise OpenCodeError("closed")
            if not self.workers:
                self.workers = [
                    asyncio.create_task(self._worker(), name=f"opencode-worker-{i}")
                    for i in range(self.cfg["max_concurrency"])
                ]

    async def submit(
        self,
        messages,
        scope,
        *,
        on_queued=None,
        sequence=None,
        stats_session=None,
        result_stats=None,
        images=None,
        on_chunk=None,
    ):
        await self._start_workers()
        now = time.monotonic()
        size = len(encode(messages))
        if size > self.cfg["context_max_bytes"]:
            raise OpenCodeError("context_limit")
        job = Job(
            messages,
            scope,
            uuid.uuid4().hex,
            asyncio.get_running_loop().create_future(),
            now,
            now + self.cfg["queue_timeout_seconds"],
            size,
        )
        job.sequence = sequence if type(sequence) is int else None
        job.stats_session = stats_session
        job.images = images
        job.on_chunk = on_chunk
        self._jobs[job.request_id] = job

        async def queue_notice():
            if on_queued is None:
                return
            try:
                await asyncio.wait_for(job.started.wait(), 3)
            except asyncio.TimeoutError:
                try:
                    await on_queued(self.queue.qsize())
                except Exception:
                    record("queue_notice_failed", request_id=job.request_id, code="notification")

        notice = asyncio.create_task(queue_notice())
        try:
            await asyncio.wait_for(self.queue.put(job), max(0.001, job.deadline - time.monotonic()))
            record("queued", request_id=job.request_id, queue_depth=self.queue.qsize())
            await asyncio.wait_for(job.started.wait(), max(0.001, job.deadline - time.monotonic()))
            reply = await job.future
            if result_stats is not None and job.tokens is not None:
                result_stats["tokens"] = job.tokens
            return reply
        except asyncio.TimeoutError:
            job.cancel.set()
            if not job.future.done():
                job.future.cancel()
            record("queue_expired", request_id=job.request_id, code="queue_timeout")
            self.last_failure = "queue_timeout"
            self._account(job, "queue_timeout")
            raise OpenCodeError("queue_timeout") from None
        except asyncio.CancelledError:
            job.cancel.set()
            if not job.future.done():
                job.future.cancel()
            if not job.started.is_set():
                self._account(job, "cancelled")
            raise
        finally:
            notice.cancel()
            if job.cancel.is_set():
                await self.queue.discard(job)
            await asyncio.gather(notice, return_exceptions=True)
            self._jobs.pop(job.request_id, None)
            if job.future.done() and not job.future.cancelled():
                job.future.exception()  # Shutdown can also wake a blocked queue.put().

    def _reuse(self, messages, scope):
        cached = self.sessions.get(scope) if scope else None
        plan = reconcile_history(
            cached["history"] if cached else None,
            messages,
            last_request_hashes=cached["request_hashes"] if cached else None,
        )
        if cached and plan["reused"]:
            return cached, plan
        return None, plan

    def _no_new_input(self, job, cached):
        # Do not send a system-only prompt or acknowledge pending system changes.
        # They are diffed against the last successfully applied state next turn.
        reply = cached["last_reply"] if cached else ""
        job.reused = cached is not None
        job.reset_reason = "cached_reply" if cached else "no_new_user_input"
        job.output_bytes = len(reply.encode())
        return reply

    async def _new_client(self):
        owner = self.owner_factory(self.cfg)
        client = None
        try:
            process, ready = await owner.start()
            client = ACPClient(
                process,
                frame_limit=max(self.cfg["context_max_bytes"] * 2, self.cfg["output_max_bytes"])
                + 65536,
                output_limit=self.cfg["output_max_bytes"],
                ready=ready,
            )
            await client.start()
            self.owner, self.client = owner, client
        except BaseException as exc:
            code = exc.code if isinstance(exc, OpenCodeError) else "cli_failed"
            if (
                client is not None
                and isinstance(exc, OpenCodeError)
                and exc.code == "process_exited"
            ):
                try:
                    await asyncio.wait_for(asyncio.shield(client._stderr_task), 1)
                except asyncio.TimeoutError:
                    pass
                if b"bwrap" in client.stderr_sample:
                    code = "sandbox_unavailable"
            try:
                if client is not None:
                    await client.close()
            finally:
                await owner.close()
            if isinstance(exc, Exception):
                record("process_start_failed", transport=owner.kind, code=code)
                raise OpenCodeError(code) from None
            raise

    async def _reset_client(self, reason):
        client, owner = self.client, self.owner
        self.client, self.owner = None, None
        self.sessions.clear()
        self.session_slots = 0
        try:
            if client is not None:
                await client.close()
        finally:
            if owner is not None:
                await owner.close()
        record("runtime_reset", reset_reason=reason)

    async def available_models(self):
        """One ACP session/new catalog query; never send a model prompt."""
        async with self._condition:
            if self.closed:
                raise OpenCodeError("closed")
            rotate = self.client is not None and (
                self.client.closed or self.session_slots >= self.cfg["max_sessions"]
            )
            if rotate:
                while self.active:
                    await self._condition.wait()
                await self._reset_client("catalog_refresh")
            if self.client is None:
                await self._new_client()
            self.session_slots += 1
            return await self.client.available_models()

    async def _lease(self, job):
        async with self._condition:
            while True:
                if self.closed or job.cancel.is_set():
                    raise OpenCodeError("closed" if self.closed else "cancelled")
                cached, _ = self._reuse(job.messages, job.scope)
                exhausted = cached is None and self.session_slots >= self.cfg["max_sessions"]
                dead = self.client is not None and self.client.closed
                if (exhausted or dead) and self.active:
                    remaining = job.deadline - time.monotonic()
                    if remaining <= 0:
                        raise OpenCodeError("queue_timeout")
                    await asyncio.wait_for(self._condition.wait(), remaining)
                    continue
                job.process_reused = self.client is not None and not self.client.closed
                if exhausted or dead:
                    await self._reset_client("session_limit" if exhausted else "process_exited")
                    job.process_reused = False
                if self.client is None:
                    await self._new_client()
                if job.images and self.owner is not None:
                    if not await self.owner.model_supports_images():
                        raise OpenCodeError("image_unsupported")
                cached, _ = self._reuse(job.messages, job.scope)
                if cached is None:
                    self.session_slots += 1
                self.active += 1
                return self.client

    async def _release(self):
        async with self._condition:
            self.active -= 1
            self._condition.notify_all()

    async def _cancel_turn(self, client, session_id, turn):
        try:
            async with asyncio.timeout(5):
                await client.notify("session/cancel", {"sessionId": session_id})
                try:
                    await asyncio.shield(turn)
                except OpenCodeError as exc:
                    if exc.code != "cancelled":
                        raise
            record("cancel_confirmed")
        except (OpenCodeError, asyncio.TimeoutError, OSError):
            # No acknowledgement: stop the transport/runtime rather than reusing
            # an unknown session. Other in-flight turns fail explicitly too.
            await client.close()
            if self.owner is not None:
                await self.owner.close()
            record("cancel_unconfirmed", code="cancel_unconfirmed")
        finally:
            if not turn.done():
                turn.cancel()
            await asyncio.gather(turn, return_exceptions=True)

    async def _execute(self, job, client):
        cached, plan = self._reuse(job.messages, job.scope)
        if job.scope in self.sessions:
            record(
                "history_comparison",
                request_id=job.request_id,
                session=job.stats_session or job.scope,
                **{
                    key: plan[key]
                    for key in ("length_rule", "hash_rule", "tail_rule", "system_changed")
                },
                **plan["history_difference"],
            )
        if not plan["has_new_user_input"]:
            return self._no_new_input(job, cached)
        delta = plan["delta"]
        job.reused = cached is not None
        job.reset_reason = (
            ("system_updated" if plan["system_changed"] else "continuous_history")
            if cached
            else ("history_changed" if job.scope in self.sessions else "new_conversation")
        )
        if not job.scope:
            job.reset_reason = "no_stable_session_key"
        session_id = cached["session_id"] if cached else await client.new_session(self.cfg["model"])
        delta, image_blocks = prompt_images(delta, job.images or {})
        job.historical_images_not_attached = sum(
            entry.get("historical_images_not_attached", 0) for entry in delta
        )
        text = render_messages(
            delta,
            incremental=bool(cached),
            clock_fields=plan["clock_fields"],
            image_count=len(image_blocks),
        )
        db_path = self.owner.usage_db if self.owner else None
        previous = (
            None if db_path is None else await asyncio.to_thread(previous_user, db_path, session_id)
        )
        job.sent_bytes = len(text.encode())
        stats = {}
        turn = asyncio.create_task(
            client.prompt(
                session_id,
                text,
                stats=stats,
                **({"images": image_blocks} if image_blocks else {}),
                **({"on_chunk": job.on_chunk} if job.on_chunk is not None else {}),
            )
        )
        try:
            completed = await asyncio.shield(turn)
            reply = completed["text"]
            job.wire_bytes = completed["wire_bytes"]
            job.output_bytes = len(reply.encode())
            if job.scope:
                self.sessions[job.scope] = {
                    "session_id": session_id,
                    "history": history_state(
                        job.messages + [{"role": "assistant", "content": reply}]
                    ),
                    "request_hashes": history_state(job.messages)["request_hashes"],
                    "last_reply": reply,
                }
            return reply
        except asyncio.CancelledError:
            if job.scope:
                self.sessions.pop(job.scope, None)
            await self._cancel_turn(client, session_id, turn)
            raise
        except BaseException:
            if job.scope:
                self.sessions.pop(job.scope, None)
            if not turn.done():
                await self._cancel_turn(client, session_id, turn)
            raise
        finally:
            if turn.done():
                await asyncio.gather(turn, return_exceptions=True)
            job.wire_bytes = stats.get("wire_bytes", job.wire_bytes)
            job.output_bytes = stats.get("output_bytes", job.output_bytes)
            job.model_id = stats.get("model_id")
            if job.wire_bytes:
                job.sent_image_blocks = len(image_blocks)
                job.sent_image_base64_bytes = sum(len(block["data"]) for block in image_blocks)
            if previous is not None and job.model_id:
                job.tokens = await asyncio.to_thread(
                    turn_tokens, db_path, session_id, previous, job.model_id
                )

    async def _worker(self):
        while True:
            job = await self.queue.get()
            leased = False
            operation = cancellation = None
            result = error = None
            code = "ok"
            try:
                if job.cancel.is_set() or job.future.cancelled():
                    code = "cancelled"
                    continue
                cached, plan = self._reuse(job.messages, job.scope)
                if not plan["has_new_user_input"]:
                    job.queue_ms = round((time.monotonic() - job.enqueued) * 1000)
                    job.process_reused = bool(self.client and not self.client.closed)
                    job.started.set()
                    result = self._no_new_input(job, cached)
                    continue
                client = await asyncio.wait_for(
                    self._lease(job), max(0.001, job.deadline - time.monotonic())
                )
                leased = True
                if job.cancel.is_set() or job.future.cancelled():
                    code = "cancelled"
                    continue
                job.queue_ms = round((time.monotonic() - job.enqueued) * 1000)
                job.started.set()
                operation = asyncio.create_task(self._execute(job, client))
                cancellation = asyncio.create_task(job.cancel.wait())
                done, _ = await asyncio.wait(
                    {operation, cancellation},
                    timeout=self.cfg["timeout_seconds"],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if operation in done:
                    result = await operation
                else:
                    code = "cancelled" if job.cancel.is_set() else "timeout"
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
                    raise OpenCodeError(code)
            except asyncio.TimeoutError:
                code = self.last_failure = "queue_timeout"
                error = OpenCodeError(code)
            except OpenCodeError as exc:
                code = self.last_failure = exc.code
                if isinstance(exc, ImageLimitError):
                    job.error_metadata = exc.metadata
                error = exc
            except asyncio.CancelledError:
                code = "closed"
                if operation is not None:
                    operation.cancel()
                    await asyncio.gather(operation, return_exceptions=True)
                error = OpenCodeError(code)
                raise
            except Exception:
                code = self.last_failure = "cli_failed"
                error = OpenCodeError(code)
            finally:
                # Wake the caller even when startup failed before a prompt began.
                job.started.set()
                if cancellation is not None:
                    cancellation.cancel()
                    await asyncio.gather(cancellation, return_exceptions=True)
                if leased:
                    await self._release()
                await self.queue.task_done(job)
                self._account(job, code)
                if not job.future.done():
                    if error is not None:
                        job.future.set_exception(error.with_traceback(None))
                    elif result is not None:
                        job.future.set_result(result)
                    else:
                        job.future.set_exception(
                            OpenCodeError(code if code != "ok" else "cancelled")
                        )

    def _account(self, job, code):
        if job.accounted:
            return
        job.accounted = True
        record(
            "request_finished",
            request_id=job.request_id,
            session=job.stats_session or job.scope,
            model_id=job.model_id,
            code=code,
            input_bytes=job.input_bytes,
            sent_input_bytes=job.sent_bytes,
            wire_bytes=job.wire_bytes,
            output_bytes=job.output_bytes,
            queue_ms=job.queue_ms
            if job.queue_ms is not None
            else round((time.monotonic() - job.enqueued) * 1000),
            duration_ms=round((time.monotonic() - job.enqueued) * 1000),
            reused=job.reused,
            process_reused=job.process_reused,
            sequence=job.sequence,
            reset_reason=job.reset_reason,
            active=self.active,
            queue_depth=self.queue.qsize(),
            sent_image_blocks=job.sent_image_blocks,
            sent_image_base64_bytes=job.sent_image_base64_bytes,
            historical_images_not_attached=job.historical_images_not_attached,
            **job.error_metadata,
            **({key: getattr(job.tokens, key) for key in TOKEN_FIELDS} if job.tokens else {}),
        )

    def health(self):
        return {
            "process_alive": bool(self.client and not self.client.closed),
            "active": self.active,
            "queue_depth": self.queue.qsize(),
            "cached_sessions": len(self.sessions),
            "max_concurrency": self.cfg["max_concurrency"],
            "last_failure": self.last_failure,
        }

    async def close(self):
        self.closed = True
        await self.queue.close()
        jobs = list(self._jobs.values())
        for job in jobs:
            job.cancel.set()
            job.started.set()
            if not job.future.done():
                job.future.set_exception(OpenCodeError("closed"))
        for worker in self.workers:
            worker.cancel()
        await asyncio.gather(*self.workers, return_exceptions=True)
        self.workers.clear()
        for job in jobs:
            self._account(job, "closed")
        await self._reset_client("shutdown")
