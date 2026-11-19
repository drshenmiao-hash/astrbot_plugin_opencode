# SPDX-License-Identifier: AGPL-3.0-only
# Source persistence and reload adapted from astrbot_plugin_openai_oauth; see NOTICE.
"""Plugin configuration and native Provider lifecycle integration."""

import asyncio
import time
from copy import deepcopy

from astrbot.api import logger

from .engine import Engine
from .errors import OpenCodeError
from .models import ModelResolutionError, resolve_model
from .settings import settings_from
from .usage import UsageStore, format_summary

PROVIDER_TYPE = "opencode_cli"
DEFAULT_SOURCE_ID = PROVIDER_TYPE
_SUSPENDED = "_opencode_suspended_for_model_change"
_settings = None
_engine = None
_available_models = None
_usage_store = None
_startup_failure = None
_validation_task = None
_retry_validation_after = 0.0
_validation_stopped = False
_STARTUP_BACKOFF = (0.5, 2.0)
_RECOVERY_COOLDOWN = 15.0
_TRANSIENT_VALIDATION_ERRORS = {
    "transport",
    "startup_timeout",
    "timeout",
    "process_exited",
    "connection_stalled",
    "ping_timeout",
    "upstream_retryable",
}


def configure_usage(context, *, directory=None):
    """Use only our plugin data subdirectory, independent of AstrBot config writes."""
    global _usage_store
    from .metrics import set_usage_store

    try:
        if directory is None:
            from pathlib import Path

            from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

            directory = Path(get_astrbot_plugin_data_path()) / "astrbot_plugin_opencode"
        existing = getattr(context, "_astrbot_opencode_usage_store", None)
        if existing is not None and not existing._closed:
            _usage_store = existing
        else:
            _usage_store = UsageStore(directory)
            context._astrbot_opencode_usage_store = _usage_store
    except Exception:
        _usage_store = None
        logger.warning("opencode_local_usage code=initialization_failed")
    set_usage_store(_usage_store)
    return _usage_store


async def _run_usage_io(operation, *args):
    task = asyncio.create_task(asyncio.to_thread(operation, *args))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        # A worker thread cannot be cancelled after it starts touching SQLite.
        await task
        raise


async def open_usage(context):
    await _run_usage_io(configure_usage, context)


async def close_usage(context):
    """Detach the writer and flush its queued records before plugin unload."""
    global _usage_store
    from .metrics import set_usage_store

    store, _usage_store = _usage_store, None
    set_usage_store(None)
    if store is not None:
        if getattr(context, "_astrbot_opencode_usage_store", None) is store:
            delattr(context, "_astrbot_opencode_usage_store")
        await _run_usage_io(store.close)


def usage_session(raw):
    return _usage_store.session_label(raw) if _usage_store is not None else None


async def local_usage(group="total", key=""):
    store = _usage_store
    if store is None:
        return "插件本地统计尚未初始化或无法写入，请检查插件数据目录权限；这不是 OpenCode 账单。"
    try:
        report = await asyncio.to_thread(store.summary, group, key)
        return format_summary(report)
    except ValueError:
        return "用法：/opencode_usage [total|session|day|model] [具体值]；日期按 UTC。这是插件本地统计。"
    except Exception:
        return "无法读取插件本地统计，请检查数据目录和统计写入错误日志；这不是 OpenCode 账单。"


async def usage_dashboard(days):
    store = _usage_store
    if store is None:
        raise OpenCodeError("usage_unavailable")
    report = await asyncio.to_thread(store.dashboard, days)
    cfg = settings()
    return {
        **report,
        "runtime": engine_health(),
        "model": cfg["model"] or cfg["requested_model"],
        "validated": bool(cfg["model"]) and _startup_failure is None,
        "startup_error": (
            {"code": _startup_failure, "message": str(OpenCodeError(_startup_failure))}
            if _startup_failure
            else None
        ),
        "concurrency_limit": cfg["max_concurrency"],
    }


def configure(config):
    global \
        _settings, \
        _available_models, \
        _startup_failure, \
        _validation_stopped, \
        _retry_validation_after
    _settings = settings_from(config)
    _settings["model"] = ""  # Never publish an unvalidated management/display ID.
    _available_models = None
    _startup_failure = None
    _validation_stopped = False
    _retry_validation_after = 0.0
    try:
        from astrbot.core.utils.astrbot_path import get_astrbot_data_path

        _settings["protected_paths"].append(get_astrbot_data_path())
    except ImportError:
        pass


def record_startup_failure(code):
    global _startup_failure
    if _startup_failure != code:
        _startup_failure = code
        logger.warning("opencode_provider_startup code=%s", code)


def _check_startup():
    if _validation_stopped:
        raise OpenCodeError("closed")
    if _startup_failure:
        raise OpenCodeError(_startup_failure)


async def _run_validation(backoff):
    global _validation_task, _startup_failure, _retry_validation_after
    try:
        for attempt, delay in enumerate((0.0, *backoff), 1):
            if delay:
                await asyncio.sleep(delay)
            try:
                await validate_configured_model()
            except OpenCodeError as exc:
                error = exc
            except asyncio.TimeoutError:
                error = OpenCodeError("startup_timeout")
            else:
                _startup_failure = None
                _retry_validation_after = 0.0
                return None
            code = error.code
            logger.warning("opencode_model_validation code=%s attempt=%d", code, attempt)
            if code not in _TRANSIENT_VALIDATION_ERRORS:
                break
        record_startup_failure(code)
        _retry_validation_after = time.monotonic() + _RECOVERY_COOLDOWN
        return error.with_traceback(None)
    finally:
        _validation_task = None


async def ensure_model_validated(*, startup=False):
    """Share an ACP probe across callers. Only startup uses the backoff sequence."""
    global _validation_task
    if _validation_stopped:
        raise OpenCodeError("closed")
    if _validation_task is None:
        if not startup and _startup_failure:
            if (
                _startup_failure not in _TRANSIENT_VALIDATION_ERRORS
                or time.monotonic() < _retry_validation_after
            ):
                raise OpenCodeError(_startup_failure)
        elif not startup and _available_models is not None:
            return
        _validation_task = asyncio.create_task(_run_validation(_STARTUP_BACKOFF if startup else ()))
    try:
        error = await asyncio.shield(_validation_task)
    except asyncio.CancelledError:
        if _validation_stopped and not asyncio.current_task().cancelling():
            raise OpenCodeError("closed") from None
        raise
    if isinstance(error, ModelResolutionError):
        raise ModelResolutionError(error.available_model_ids)
    if error is not None:
        raise OpenCodeError(error.code)


async def stop_validation():
    global _validation_stopped, _validation_task
    _validation_stopped = True
    task = _validation_task
    if task is not None:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if _validation_task is task:
            _validation_task = None


async def _fetch_available_models():
    # OpenCode can probe in its own sandbox without interrupting live chats.
    probe = Engine({**settings(), "model": _settings["requested_model"]})
    try:
        return await asyncio.wait_for(probe.available_models(), _settings["queue_timeout_seconds"])
    finally:
        closing = asyncio.create_task(probe.close())
        try:
            await asyncio.shield(closing)
        except asyncio.CancelledError:
            await closing
            raise


async def validate_configured_model():
    """Fetch one ACP catalog before registration, without a session/prompt call."""
    global _available_models
    requested = settings()["requested_model"]
    _settings["model"] = ""
    if not requested:
        _available_models = []
        return None
    _available_models = None
    _available_models = await _fetch_available_models()
    resolved = _resolve_catalog_model(requested)
    _settings["model"] = resolved
    logger.info("OpenCode model_id 已通过 ACP 校验，实际 modelId=%s", resolved)
    return resolved


def resolve_model_id(requested):
    _check_startup()
    return _resolve_catalog_model(requested)


def _resolve_catalog_model(requested):
    if _available_models is None:
        raise OpenCodeError("model_not_validated")
    try:
        return resolve_model(requested, _available_models)
    except ModelResolutionError as exc:
        # These errors contain only validated model IDs, never CLI error payloads.
        logger.warning("%s", exc)
        raise


async def available_model_ids():
    await ensure_model_validated()
    model = settings()["model"]
    return [model] if model else []


def settings():
    if _settings is None:
        raise OpenCodeError("closed")
    return dict(_settings)


def acquire_engine():
    global _engine
    if (
        _engine is None
        or _engine.closed
        or any(
            _engine.cfg.get(key) != value
            for key, value in settings().items()
            if key != "requested_model"
        )
    ):
        _engine = Engine(settings())
    _engine.references += 1
    return _engine


async def release_engine(engine):
    global _engine
    engine.references -= 1
    if engine.references == 0:
        if not engine.closed:
            await engine.close()
        if _engine is engine:
            _engine = None


async def stop_providers(context):
    manager = getattr(context, "provider_manager", None)
    providers = [
        instance
        for instance in getattr(manager, "inst_map", {}).values()
        if getattr(instance, "_astrbot_opencode_plugin", False) is True
    ]
    # Close shared engines before terminating adapters so pending callers see
    # a provider error and AstrBot can apply its usual fallback.
    engines = {p.engine for p in providers if p.engine is not None and not p.engine.closed}
    if _engine is not None and not _engine.closed:
        engines.add(_engine)
    results = await asyncio.gather(*(engine.close() for engine in engines), return_exceptions=True)
    results.extend(
        await asyncio.gather(
            *(provider.terminate() for provider in providers), return_exceptions=True
        )
    )
    for failure in results:
        if isinstance(failure, BaseException):
            raise failure


def engine_health():
    return (
        _engine.health()
        if _engine is not None
        else {"process_alive": False, "active": 0, "queue_depth": 0, "cached_sessions": 0}
    )


def _build_default_source_config():
    return {
        "id": DEFAULT_SOURCE_ID,
        "provider": "opencode",
        "type": PROVIDER_TYPE,
        "provider_type": "chat_completion",
        "enable": True,
        "key": "",
        "api_base": "",
        "modalities": ["text"],
    }


async def _commit_configuration(config, updates):
    """Reference ProviderRegistry._commit_configuration, without account storage."""
    missing = object()
    changes = {key: value for key, value in updates.items() if config.get(key, missing) != value}
    if not changes:
        return
    previous = {key: deepcopy(config[key]) if key in config else missing for key in changes}
    proposed = deepcopy(changes)
    config.update(changes)

    async def save():
        try:
            if (
                await config.save_config_async() is False
                and await config.save_config_async() is False
            ):
                raise OpenCodeError("registration_save_failed")
        except BaseException as exc:
            for key, value in previous.items():
                # As in the reference plugin, do not overwrite a concurrent edit.
                if config.get(key) != proposed[key]:
                    continue
                if value is missing:
                    config.pop(key, None)
                else:
                    config[key] = value
            if not isinstance(exc, Exception):
                raise
            raise OpenCodeError("registration_save_failed") from None

    # AstrBot writes the config in a worker thread. The write can still commit
    # after its awaiter is cancelled, so let its own success/rollback finish.
    saving = asyncio.create_task(save())
    try:
        await asyncio.shield(saving)
    except asyncio.CancelledError:
        await asyncio.gather(saving, return_exceptions=True)
        raise


async def ensure_default_source_and_model(config):
    """Publish only our source/model; never change model selection or fallbacks."""
    sources = deepcopy(config.get("provider_sources", []))
    models = deepcopy(config.get("provider", []))
    owned_sources = [s for s in sources if s.get("type") == PROVIDER_TYPE]
    source = next((s for s in owned_sources if s["id"] == DEFAULT_SOURCE_ID), None)
    source = source or next(iter(owned_sources), None)
    if source is None:
        if any(s.get("id") == DEFAULT_SOURCE_ID for s in sources):
            raise OpenCodeError("source_id_conflict")
        source = _build_default_source_config()
        sources.append(source)
        owned_sources.append(source)
    for item in owned_sources:
        item.update(key="", api_base="", provider_type="chat_completion", modalities=["text"])
        item.setdefault("enable", True)
    source_ids = {s["id"] for s in owned_sources}

    def owned(model):
        sid = model.get("provider_source_id")
        return sid in source_ids or (not sid and model.get("type") == PROVIDER_TYPE)

    for item in models:
        if owned(item):
            item.update(key="", api_base="", modalities=["text"])
    model_id = settings()["model"]
    target = None
    if not model_id:
        logger.warning(
            "OpenCode model_id 为空或设置为 auto/default，未创建原生模型；"
            "请填写明确的 provider/model 后重载插件。不会自动选模型。"
        )
    else:
        native_id = f"{PROVIDER_TYPE}:{model_id}"
        target = next((m for m in models if owned(m) and m.get("id") == native_id), None)
        target = target or next(
            (m for m in models if owned(m) and m.get("model") == model_id), None
        )
        if target is None and any(m.get("id") == native_id for m in models):
            logger.warning(
                "OpenCode 原生模型 ID 已被其他提供商占用，未创建或覆盖模型；"
                "请先处理 ID 冲突。不会自动改名或选模型。"
            )
        else:
            if target is None:
                target = {"id": native_id, "enable": source.get("enable", True)}
                models.append(target)
            elif target["id"] != native_id:
                logger.info(
                    "OpenCode 已存在相同模型，沿用其原生 ID，不重复创建或重命名；"
                    "保留默认模型与备用模型引用。"
                )
            target.update(
                model=model_id,
                provider_source_id=source["id"],
                type=PROVIDER_TYPE,
                provider_type="chat_completion",
                key="",
                api_base="",
                modalities=["text", "image"],
            )
            target.setdefault("enable", True)
            if target.get(_SUSPENDED) and source.get("enable", True):
                target.pop(_SUSPENDED)
                target["enable"] = True

    # Older versions left their generated entries enabled even when pinned to
    # a different model. Keep their IDs and settings so references still exist
    # and only restore entries that the plugin itself disabled.
    for item in models:
        if (
            item is not target
            and owned(item)
            and item.get("model") != model_id
            and item.get("id") == f"{PROVIDER_TYPE}:{item.get('model')}"
            and item.get("enable", True)
        ):
            item["enable"] = False
            item[_SUSPENDED] = True
    await _commit_configuration(config, {"provider_sources": sources, "provider": models})
    return target["id"] if target else None


async def initialize(context):
    """Ensure native config, then refresh instances with the OAuth plugin's lifecycle."""
    config = context.astrbot_config_mgr.default_conf
    try:
        await ensure_model_validated(startup=True)
    except OpenCodeError:
        # Replace stopped adapters so subsequent requests use this runtime's validation state.
        await _reload_host_models(context, None, stopped_only=True)
        raise
    target_id = await ensure_default_source_and_model(config)
    await _reload_host_models(context, target_id)


async def _reload_host_models(context, target_id, *, stopped_only=False):
    config = context.astrbot_config_mgr.default_conf
    manager = getattr(context, "provider_manager", None)
    if manager is None:
        return
    # Same on_change synchronization as the reference plugin's coordinator.
    manager.provider_sources_config = config.get("provider_sources", [])
    manager.providers_config = config.get("provider", [])
    source_ids = {
        source["id"]
        for source in config.get("provider_sources", [])
        if source.get("type") == PROVIDER_TYPE
    }
    instances = getattr(manager, "inst_map", {})
    selected = getattr(manager, "curr_provider_inst", None)
    selected_id = next((key for key, instance in instances.items() if instance is selected), None)
    # On cold start AstrBot loads providers after plugin.initialize(). Its MCP
    # initialization task also distinguishes a running manager with zero models.
    running = bool(instances) or getattr(manager, "_mcp_init_task", None) is not None
    try:
        for model in list(config.get("provider", [])):
            own_model = model.get("provider_source_id") in source_ids or (
                not model.get("provider_source_id") and model.get("type") == PROVIDER_TYPE
            )
            should_load = model["id"] in instances or (
                running and model["id"] == target_id and model.get("enable", True)
            )
            if stopped_only:
                instance = instances.get(model["id"])
                should_load = getattr(
                    instance, "_astrbot_opencode_plugin", False
                ) is True and getattr(instance, "_closed", False)
            if not own_model or not should_load:
                continue
            from .provider import OpenCodeCLIProvider

            await manager.reload(model)
            if model.get("enable", True) and not isinstance(
                instances.get(model["id"]), OpenCodeCLIProvider
            ):
                raise OpenCodeError("config")
    finally:
        # If the prior instance was disabled, don't restore a terminated object.
        # The configured default ID stays in AstrBot's settings.
        manager.curr_provider_inst = instances.get(selected_id) if selected_id else selected
