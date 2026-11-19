"""Bounded ACP v1 stdio client. One reader dispatches concurrent session replies."""

import asyncio
import json
import time

from astrbot.api import logger

from .errors import ImageLimitError, OpenCodeError, OpenCodeStreamError
from .images import MAX_IMAGE_FRAME_BYTES
from .launcher import CHAT_MODE
from .logic import (
    classify_chunk_tail_error,
    classify_error,
    classify_image_error,
    classify_reply_error,
)
from .models import ModelResolutionError, resolve_model


class ACPClient:
    def __init__(self, process, *, frame_limit, output_limit, ready=None):
        self.process = process
        self.frame_limit = frame_limit
        self.output_limit = output_limit
        self.ready = ready or {"cwd": "/work", "transport": "local"}
        self.initialized = None
        self.pending = {}
        self.turns = {}
        self.session_models = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._discard_stderr())
        self.closed = False
        self.failure_code = None
        self.stderr_sample = bytearray()
        self.last_activity = time.monotonic()
        self._ready_future = asyncio.get_running_loop().create_future()
        if ready is not None:
            self._ready_future.set_result(ready)

    async def _discard_stderr(self):
        # Retain at most 8 KiB privately for dependency error classification.
        # This buffer is never logged, returned to chat, or written to disk.
        while chunk := await self.process.stderr.read(16384):
            if len(self.stderr_sample) < 8192:
                self.stderr_sample.extend(chunk[: 8192 - len(self.stderr_sample)])

    async def _write(self, message, *, image_prompt=False):
        raw = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"
        limit = self.frame_limit + (MAX_IMAGE_FRAME_BYTES if image_prompt else 0)
        if len(raw) > limit:
            if image_prompt:
                raise ImageLimitError("frame_bytes", limit, len(raw))
            raise OpenCodeError("context_limit")
        async with self._write_lock:
            try:
                self.process.stdin.write(raw)
                await self.process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                raise OpenCodeError("process_exited") from None
        return len(raw)

    async def call(self, method, params, *, timeout=None, image_prompt=False):
        if self.closed:
            raise OpenCodeError(self.failure_code or "process_exited")
        self._next_id += 1
        request_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            sent_bytes = await self._write(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params},
                image_prompt=image_prompt,
            )
            if method == "session/prompt" and params.get("sessionId") in self.turns:
                self.turns[params["sessionId"]]["wire_bytes"] = sent_bytes
            if timeout is None:
                return await future
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise OpenCodeError("startup_timeout") from None
        finally:
            self.pending.pop(request_id, None)
            if not future.done():
                future.cancel()

    async def notify(self, method, params):
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def start(self):
        try:
            await asyncio.wait_for(asyncio.shield(self._ready_future), 20)
            self.initialized = await self.call(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {
                        "fs": {"readTextFile": False, "writeTextFile": False},
                        "terminal": False,
                    },
                    "clientInfo": {"name": "astrbot_plugin_opencode", "version": "0.2.2"},
                },
                timeout=30,
            )
            if self.initialized.get("protocolVersion") != 1:
                raise OpenCodeError("protocol")
        except asyncio.TimeoutError:
            raise OpenCodeError("startup_timeout") from None

    @staticmethod
    def _option(result, option_id):
        options = result.get("configOptions")
        if not isinstance(options, list):
            raise OpenCodeError("protocol")
        return next(
            (item for item in options if isinstance(item, dict) and item.get("id") == option_id), {}
        )

    async def _open_session(self):
        result = await self.call(
            "session/new", {"cwd": self.ready["cwd"], "mcpServers": []}, timeout=30
        )
        if not isinstance(result.get("sessionId"), str) or not result["sessionId"]:
            raise OpenCodeError("protocol")
        if self._option(result, "mode").get("currentValue") != CHAT_MODE:
            raise OpenCodeError("chat_mode_required")
        return result

    @classmethod
    def _models(cls, result):
        options = cls._option(result, "model").get("options")
        if not isinstance(options, list):
            raise OpenCodeError("protocol")
        return [
            item["value"]
            for item in options
            if isinstance(item, dict) and isinstance(item.get("value"), str)
        ]

    async def available_models(self):
        return self._models(await self._open_session())

    async def new_session(self, model):
        result = await self._open_session()
        session_id = result["sessionId"]
        try:
            model = resolve_model(model, self._models(result))
        except ModelResolutionError as exc:
            logger.warning("%s", exc)
            raise
        selected = await self.call(
            "session/set_config_option",
            {"sessionId": session_id, "configId": "model", "value": model},
            timeout=30,
        )
        if self._option(selected, "model").get("currentValue") != model:
            raise OpenCodeError("model_mismatch")
        if self._option(selected, "mode").get("currentValue") != CHAT_MODE:
            raise OpenCodeError("chat_mode_required")
        self.session_models[session_id] = model
        return session_id

    async def prompt(self, session_id, text, *, stats=None, images=()):
        if images:
            capabilities = (self.initialized or {}).get("agentCapabilities")
            supported = (
                capabilities.get("promptCapabilities") if isinstance(capabilities, dict) else None
            )
            if not isinstance(supported, dict) or supported.get("image") is not True:
                raise OpenCodeError("image_unsupported")
        state = {"parts": [], "size": 0, "error": None}
        self.turns[session_id] = state
        try:
            result = await self.call(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": text}, *images],
                },
                **({"image_prompt": True} if images else {}),
            )
            if state["error"]:
                raise OpenCodeError(state["error"])
            if result.get("stopReason") == "cancelled":
                raise OpenCodeError("cancelled")
            if result.get("stopReason") not in {
                "end_turn",
                "max_tokens",
                "max_turn_requests",
                "refusal",
            }:
                raise OpenCodeError("cli_failed")
            text = "".join(state["parts"])
            if not text.strip():
                raise OpenCodeError("empty_output")
            if images:
                image_error = classify_image_error(text, reply=True) or classify_image_error(
                    state["parts"][-1], reply=True
                )
                if image_error:
                    raise OpenCodeError(image_error)
            error_code = classify_reply_error(text) or classify_chunk_tail_error(
                text, state["parts"]
            )
            if error_code:
                raise OpenCodeStreamError(error_code)
            return {"text": text, "wire_bytes": state.get("wire_bytes", 0)}
        except OpenCodeStreamError:
            raise
        except OpenCodeError as exc:
            if exc.code in {"connection_stalled", "ping_timeout", "upstream_retryable"}:
                raise OpenCodeStreamError(exc.code) from None
            raise
        finally:
            if stats is not None:
                stats.update(wire_bytes=state.get("wire_bytes", 0), output_bytes=state["size"])
                stats["model_id"] = (
                    self.session_models.get(session_id) if state.get("wire_bytes", 0) else None
                )
            self.turns.pop(session_id, None)
            self.last_activity = time.monotonic()

    async def _server_request(self, message):
        method = message.get("method")
        params = message.get("params", {})
        if method == "session/request_permission":
            rejected = next(
                (
                    o.get("optionId")
                    for o in params.get("options", [])
                    if o.get("kind") == "reject_once"
                ),
                None,
            )
            result = (
                {"outcome": {"outcome": "selected", "optionId": rejected}}
                if rejected
                else {"outcome": {"outcome": "cancelled"}}
            )
        else:
            await self._write(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {"code": -32601, "message": "Client capability unavailable"},
                }
            )
            return
        await self._write({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def _update(self, params):
        state = self.turns.get(params.get("sessionId"))
        if state is None:
            return
        update = params.get("update", {})
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk":
            content = update.get("content", {})
            if content.get("type") != "text" or not isinstance(content.get("text"), str):
                state["error"] = "unsupported_output"
                return not state.get("cancel_sent", False)
            text = content["text"]
            state["size"] += len(text.encode())
            if state["size"] > self.output_limit:
                state["error"] = "output_limit"
                return not state.get("cancel_sent", False)
            state["parts"].append(text)
        elif kind == "current_mode_update" and update.get("currentModeId") != CHAT_MODE:
            state["error"] = "chat_mode_required"
        elif kind == "config_option_update":
            mode = self._option(update, "mode")
            if mode and mode.get("currentValue") != CHAT_MODE:
                state["error"] = "chat_mode_required"
        elif kind in {"tool_call", "tool_call_update"}:
            state["error"] = "tool_blocked"
        return bool(state["error"]) and not state.get("cancel_sent", False)

    async def _read_loop(self):
        failure = "process_exited"
        try:
            while True:
                raw = await self.process.stdout.readline()
                if not raw:
                    break
                if len(raw) > self.frame_limit:
                    raise OpenCodeError("output_limit")
                message = json.loads(raw)
                if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
                    raise OpenCodeError("protocol")
                method = message.get("method")
                if method == "session/update":
                    params = message.get("params", {})
                    if self._update(params):
                        self.turns[params["sessionId"]]["cancel_sent"] = True
                        await self.notify("session/cancel", {"sessionId": params["sessionId"]})
                elif method and "id" in message:
                    await self._server_request(message)
                elif "id" in message:
                    future = self.pending.get(message["id"])
                    if future is None or future.done():
                        continue
                    if "error" in message:
                        error = message["error"]
                        # ACP -32000 means authentication required. Never expose
                        # raw RPC error data, which can include prompts or URLs.
                        if error.get("code") == -32000:
                            code = "not_logged_in"
                        else:
                            message_text = str(error.get("message", ""))
                            code = classify_error(message_text)
                            if code in {"cli_failed", "timeout"}:
                                code = classify_reply_error(message_text) or code
                        future.set_exception(OpenCodeError(code))
                    elif isinstance(message.get("result"), dict):
                        future.set_result(message["result"])
                    else:
                        future.set_exception(OpenCodeError("protocol"))
        except asyncio.CancelledError:
            return
        except OpenCodeError as exc:
            failure = exc.code
        except (ValueError, UnicodeError, RecursionError, TypeError, AttributeError):
            failure = "protocol"
        finally:
            self.closed = True
            self.failure_code = failure
            if not self._ready_future.done():
                self._ready_future.set_exception(OpenCodeError(failure))
            for future in list(self.pending.values()):
                if not future.done():
                    future.set_exception(OpenCodeError(failure))

    async def close(self):
        self.closed = True
        if not self._ready_future.done():
            self._ready_future.cancel()
        for task in (self._reader_task, self._stderr_task):
            task.cancel()
        await asyncio.gather(self._reader_task, self._stderr_task, return_exceptions=True)
        for future in list(self.pending.values()):
            if not future.done():
                future.set_exception(OpenCodeError("closed"))
        # The owner closes the process and removes its private session data.
