"""Optional install/update and restart through AstrBot's real managers and OpenCode."""

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

PLUGIN = "astrbot_plugin_opencode"
MODEL = "deepseek/deepseek-flash"
NATIVE = f"opencode_cli:{MODEL}"


@unittest.skipUnless(os.getenv("OPENCODE_TEST_BIN"), "Set OPENCODE_TEST_BIN for host integration")
class HostInstallTests(unittest.TestCase):
    def test_install_upgrade_chat_and_restart(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            host = Path(tmp)
            release = host / "release.zip"
            if supplied := os.getenv("OPENCODE_TEST_ZIP"):
                shutil.copyfile(supplied, release)
            else:
                with zipfile.ZipFile(release, "w") as archive:
                    for name in (
                        "main.py",
                        "metadata.yaml",
                        "_conf_schema.json",
                        "requirements.txt",
                        "README.md",
                        "opencode_plugin",
                        ".astrbot-plugin",
                        "views",
                    ):
                        source = root / name
                        for file in source.rglob("*") if source.is_dir() else [source]:
                            if file.is_file() and "__pycache__" not in file.parts:
                                archive.write(file, PLUGIN + "/" + str(file.relative_to(root)))
            env = {**os.environ, "ASTRBOT_ROOT": tmp, "ASTRBOT_DISABLE_METRICS": "1"}
            for phase in ("install", "restart"):
                subprocess.run(
                    [sys.executable, __file__, phase], cwd=tmp, env=env, check=True, timeout=120
                )


async def exercise_host(phase):
    from aiohttp import web
    from astrbot.core import astrbot_config, db_helper, sp
    from astrbot.core.astrbot_config_mgr import AstrBotConfigManager
    from astrbot.core.conversation_mgr import ConversationManager
    from astrbot.core.provider.entities import ProviderType
    from astrbot.core.provider.manager import ProviderManager
    from astrbot.core.star.context import Context
    from astrbot.core.star.star_manager import PluginManager
    from astrbot.core.umop_config_router import UmopConfigRouter
    from astrbot.dashboard.services.plugin_page_service import PluginPageService

    root = Path(os.environ["ASTRBOT_ROOT"])
    sys.path.insert(0, str(root))
    await db_helper.initialize()
    configs = AstrBotConfigManager(astrbot_config, UmopConfigRouter(sp), sp)
    await configs.initialize()
    providers = ProviderManager(configs, db_helper, SimpleNamespace(default_persona=None))
    conversations = ConversationManager(db_helper)
    origin = "test:FriendMessage:user"
    ctx = Context(
        asyncio.Queue(),
        astrbot_config,
        db_helper,
        providers,
        None,
        conversations,
        None,
        None,
        configs,
        None,
        None,
    )
    plugins = PluginManager(ctx, astrbot_config)
    release = root / "release.zip"
    requests = []
    hold_reply = asyncio.Event()
    hold_reply.set()

    async def model(request):
        assert request.headers["Authorization"] == "Bearer fixture-only"
        payload = await request.json()
        assert not payload.get("tools")
        requests.append(payload)
        response = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await response.prepare(request)
        for word in ("real", " reply", ""):
            chunk = {
                "id": "fixture",
                "object": "chat.completion.chunk",
                "created": 1,
                "model": "deepseek-flash",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": word},
                        "finish_reason": None if word else "stop",
                    }
                ],
            }
            try:
                await response.write(f"data: {json.dumps(chunk)}\n\n".encode())
                await asyncio.wait_for(hold_reply.wait(), 15)
            except ConnectionError:
                return response
        await response.write(b"data: [DONE]\n\n")
        return response

    async def archive(_):
        return web.FileResponse(release)

    app = web.Application()
    app.router.add_post("/v1/chat/completions", model)
    app.router.add_get("/release.zip", archive)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", 0).start()
    url = f"http://127.0.0.1:{runner.addresses[0][1]}"
    spawn = asyncio.create_subprocess_exec

    async def fixture_upstream(*args, **kwargs):
        env = kwargs.get("env", {})
        if "OPENCODE_CONFIG_CONTENT" in env:
            config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
            config.setdefault("provider", {}).setdefault("deepseek", {})["options"] = {
                "baseURL": url + "/v1"
            }
            kwargs["env"] = {**env, "OPENCODE_CONFIG_CONTENT": json.dumps(config)}
        return await spawn(*args, **kwargs)

    try:
        with patch("asyncio.create_subprocess_exec", fixture_upstream):
            if phase == "install":
                await providers.initialize()
                upload = root / "upload.zip"
                shutil.copyfile(os.getenv("OPENCODE_TEST_PREVIOUS_ZIP") or release, upload)
                await plugins.install_plugin_from_file(str(upload))
                auth = root / "auth.json"
                auth.write_text(json.dumps({"deepseek": {"type": "api", "key": "fixture-only"}}))
                auth.chmod(0o600)
                star = ctx.get_registered_star(PLUGIN)
                star.config.update(
                    model_id=MODEL, cli_binary=os.environ["OPENCODE_TEST_BIN"], auth_path=str(auth)
                )
                await star.config.save_config_async()
                assert (await plugins.reload(PLUGIN))[0]
                first = providers.inst_map[NATIVE]
                await providers.set_provider(NATIVE, ProviderType.CHAT_COMPLETION, umo=origin)
                cid = await conversations.new_conversation(origin)
                initial = await first.text_chat(prompt="prior question", conversation_id=cid)
                assert initial.completion_text == "real reply"
                await conversations.update_conversation(
                    origin,
                    cid,
                    history=[
                        {"role": "user", "content": "prior question"},
                        {"role": "assistant", "content": initial.completion_text},
                    ],
                )
                old_process = first.engine.owner.process
                old_scratch = Path(first.engine.owner.scratch.name)
                await plugins.update_plugin(PLUGIN, download_url=url + "/release.zip")
                assert old_process.returncode is not None and not old_scratch.exists()
                assert providers.inst_map[NATIVE] is not first
            else:
                assert (await plugins.load(specified_dir_name=PLUGIN))[0]
                await providers.initialize()

            current = await ctx.get_using_provider_async(umo=origin)
            assert current is providers.inst_map[NATIVE]
            cid = await conversations.get_curr_conversation_id(origin)
            assert cid
            conversation = await conversations.get_conversation(origin, cid)
            history = json.loads(conversation.history)
            hold_reply.clear()
            stream = current.text_chat_stream(
                prompt="follow up", contexts=history, conversation_id=cid
            )
            try:
                chunk = await asyncio.wait_for(anext(stream), 15)
                assert chunk.is_chunk and chunk.completion_text == "real"
                star = ctx.get_registered_star(PLUGIN)
                engine = current.engine
                active_process = engine.owner.process
                before_requests = len(requests)
                report = await star.star_cls._environment_page.checks.read(refresh=True)
                items = {r["id"]: r for r in report["items"]}
                assert items["sandbox"]["status"] == ("pass" if phase == "install" else "disabled")
                assert all(items[k]["status"] == "pass" for k in ("cli", "auth", "model")), items
                assert len(requests) == before_requests
                assert current.engine is engine and engine.owner.process is active_process
                hold_reply.set()
                rest = [item async for item in stream]
                assert [(item.is_chunk, item.completion_text) for item in rest] == [
                    (True, " reply"),
                    (False, "real reply"),
                ]
            finally:
                hold_reply.set()
                await stream.aclose()
            assert "prior question" in requests[-1]["messages"][-1]["content"]
            assert len(providers.provider_insts) == 1
            if phase == "install":
                engine = current.engine
                hold_reply.clear()
                interrupted = current.text_chat_stream(
                    prompt="cancel me", conversation_id="another"
                )
                try:
                    assert (
                        await asyncio.wait_for(anext(interrupted), 15)
                    ).completion_text == "real"
                    await interrupted.aclose()
                    async with asyncio.timeout(8):
                        while engine.active:
                            await asyncio.sleep(0.01)
                    assert not engine.client.closed and not engine.client.pending
                finally:
                    hold_reply.set()
                    await interrupted.aclose()
                old_process = engine.owner.process
                hold_reply.clear()
                crashed = current.text_chat_stream(prompt="crash test", conversation_id="crash")
                try:
                    assert (await asyncio.wait_for(anext(crashed), 15)).is_chunk
                    old_process.kill()
                    with unittest.TestCase().assertRaises(
                        ctx.get_registered_star(PLUGIN).module.OpenCodeError
                    ) as failure:
                        await asyncio.wait_for(anext(crashed), 5)
                    assert failure.exception.code == "process_exited"
                    await asyncio.wait_for(old_process.wait(), 5)
                finally:
                    hold_reply.set()
                    await crashed.aclose()
                rebuilt = await current.text_chat(
                    prompt="recover", contexts=history, conversation_id=cid
                )
                assert rebuilt.completion_text == "real reply"
                assert engine.owner.process is not old_process
                assert "prior question" in requests[-1]["messages"][-1]["content"]
                await plugins.turn_off_plugin(PLUGIN)
                assert NATIVE not in providers.inst_map and not providers.provider_insts
                await plugins.turn_on_plugin(PLUGIN)
                assert NATIVE in providers.inst_map
            star = ctx.get_registered_star(PLUGIN)
            page = await PluginPageService(plugins).get_plugin_page(star, "environment")
            assert page.name == "environment"
            # Both directions use the real host's plugin-scoped reload.
            current = providers.inst_map[NATIVE]
            old_process = current.engine.owner.process if current.engine.owner else None
            star.config["sandbox_enabled"] = phase != "install"
            await star.config.save_config_async()
            assert (await plugins.reload(PLUGIN))[0]
            if old_process:
                assert old_process.returncode is not None
            current = await ctx.get_using_provider_async(umo=origin)
            assert current is providers.inst_map[NATIVE]
            hold_reply.clear()
            stream = current.text_chat_stream(prompt="after mode change", conversation_id="switch")
            try:
                assert (await asyncio.wait_for(anext(stream), 15)).is_chunk
                active = current.engine.owner.process
                scratch = Path(current.engine.owner.scratch.name)
                await stream.aclose()
                async with asyncio.timeout(8):
                    while current.engine.active:
                        await asyncio.sleep(0.01)
                assert not current.engine.client.closed and not current.engine.client.pending
            finally:
                hold_reply.set()
                await stream.aclose()
            await plugins.turn_off_plugin(PLUGIN)
            assert active.returncode is not None and not scratch.exists()
            await plugins.turn_on_plugin(PLUGIN)
            star = ctx.get_registered_star(PLUGIN)
            page = await PluginPageService(plugins).get_plugin_page(star, "usage")
            assert page.name == "usage"
            report = await star.module.runtime.usage_dashboard(7)
            assert report["validated"] and report["totals"]["requests"] >= (
                3 if phase == "install" else 4
            )
    finally:
        hold_reply.set()
        star = ctx.get_registered_star(PLUGIN)
        if star and star.activated:
            await plugins._terminate_plugin(star)
        await providers.terminate()
        await runner.cleanup()
        await db_helper.engine.dispose()


if __name__ == "__main__":
    asyncio.run(exercise_host(sys.argv[1]))
