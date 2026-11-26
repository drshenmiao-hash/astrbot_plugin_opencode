"""Read-only, single-flight diagnostics for the loaded plugin configuration."""

import asyncio
import os
import platform
import re
import shutil
import signal
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from astrbot import __version__ as astrbot_version
from packaging.version import InvalidVersion, Version

from . import runtime
from .errors import MESSAGES, OpenCodeError
from .launcher import read_api_key, sandbox_command, sandbox_prefix
from .protocol import explicit_model
from .settings import PATH_DEFAULTS, configured_path

BASELINE = "1.19.6"
PROBE_TIMEOUT = 4
MAX_OUTPUT = 4096
VERSION = re.compile(rb"v?(\d{1,5}\.\d{1,5}\.\d{1,5})([-+][a-zA-Z0-9.+-]{1,64})?")
TITLES = {
    "system": "运行环境",
    "cli": "OpenCode CLI",
    "sandbox": "bubblewrap 沙盒",
    "auth": "服务商认证",
    "model": "模型与 ACP",
}


def item(name, status, detail, facts=None):
    return {
        "id": name,
        "title": TITLES[name],
        "status": status,
        "detail": detail,
        "facts": facts or {},
    }


def system_check():
    linux = sys.platform == "linux"
    container = Path("/.dockerenv").is_file() or Path("/run/.containerenv").is_file()
    facts = {
        "操作系统": platform.system(),
        "架构": platform.machine(),
        "Python": platform.python_version(),
        "AstrBot": astrbot_version,
        "容器": "已发现容器标记" if container else "未确认",
    }
    if not linux or sys.version_info < (3, 12):
        return item("system", "action", "请在 Linux、Python 3.12 或更高版本中运行 AstrBot。", facts)
    try:
        version = Version(astrbot_version)
    except InvalidVersion:
        return item("system", "unknown", "无法确认 AstrBot 版本，请核对插件要求。", facts)
    if not (Version("4.29.4") <= version and version.major == 4):
        return item(
            "system", "action", "请使用符合插件版本要求的 AstrBot（4.29.4 至 5.0 之前）。", facts
        )
    return item("system", "pass", "当前 AstrBot 进程符合系统和宿主版本要求。", facts)


def _high_privilege():
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return True
    try:
        with open("/proc/self/status", "rb") as status:
            caps = re.search(rb"^CapEff:\s*([0-9a-fA-F]+)$", status.read(16384), re.M)
        return bool(caps and int(caps[1], 16))
    except OSError:
        return False


def _path(config, name):
    try:
        return configured_path(config, name)
    except (ValueError, RuntimeError, OSError):
        return None


def _find(config, field, command):
    path = _path(config, field)
    regular = bool(path and os.path.isfile(path))
    configured = regular and os.access(path, os.X_OK)
    # which() can return relative entries when PATH contains relative components.
    candidate = shutil.which(command)
    candidate = os.path.abspath(candidate) if candidate else None
    facts = {
        "配置路径": path or "无效，请填写绝对路径",
        "配置检查": "可执行" if configured else ("不可执行" if regular else "未找到可执行文件"),
    }
    if candidate and (not configured or os.path.realpath(path) != os.path.realpath(candidate)):
        facts["PATH 候选"] = candidate
    actual = path if configured else candidate
    if actual:
        facts["本次检测路径"] = actual
    return actual, configured, facts


async def _probe(argv, cwd, *, isolated=False):
    """Run a fixed command with no inherited credentials and bounded stdout/lifetime."""
    env = {
        "HOME": "/home/opencode" if isolated else str(cwd),
        "TMPDIR": "/tmp" if isolated else str(cwd),
        "PATH": "/usr/bin:/bin",
        "LANG": "C.UTF-8",
        "OPENCODE_DISABLE_AUTOUPDATE": "1",
        "OPENCODE_DISABLE_MODELS_FETCH": "1",
        "OPENCODE_DISABLE_DEFAULT_PLUGINS": "1",
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_TEST_MANAGED_CONFIG_DIR": str(cwd / ".managed"),
    }
    process = None
    spawn = asyncio.create_task(
        asyncio.create_subprocess_exec(
            sys.executable,
            "-I",
            "-S",
            str(Path(__file__).with_name("_probe_exec.py")),
            *argv,
            cwd=cwd,
            env=env,
            start_new_session=True,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=MAX_OUTPUT,
        )
    )
    try:
        async with asyncio.timeout(PROBE_TIMEOUT):
            process = await asyncio.shield(spawn)
            output = bytearray()
            while chunk := await process.stdout.read(MAX_OUTPUT + 1):
                output.extend(chunk)
                if len(output) > MAX_OUTPUT:
                    return "limit", b""
            code = await process.wait()
            return ("ok", bytes(output).strip()) if code == 0 else ("failed", b"")
    except TimeoutError:
        return "timeout", b""
    except OSError:
        return "failed", b""
    finally:
        if process is None:
            # Cancellation may race with asyncio assigning the child's PID.
            try:
                process = await spawn
            except OSError:
                pass
        if process is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.stdout._transport.close()
            await process.wait()


async def binary_check(config, name, directory, protected, sandbox=None):
    is_cli = name == "cli"
    field, command = ("cli_binary", "opencode") if is_cli else ("bubblewrap_binary", "bwrap")
    actual, configured, facts = _find(config, field, command)
    if not actual:
        detail = f"请在 AstrBot 实际运行的 Linux 系统或容器中安装 {command}，并核对 {field} 的路径和执行权限。"
        return item(name, "action", detail, facts)
    if sys.platform != "linux":
        return item(name, "unknown", "仅在支持的 Linux 环境执行检测。", facts)
    argv = [actual]
    isolated = is_cli and config.get("sandbox_enabled", True) is not False
    if isolated:
        if not sandbox or sandbox["facts"].get("隔离能力") != "已验证":
            detail = "已找到可执行文件。沙盒可用后将执行版本检查；兼容性未验证。"
            if not configured:
                detail += f" 当前为 PATH 候选，请配置 {field}，保存并重载。"
            return item(name, "unknown" if configured else "action", detail, facts)
        prefix = sandbox_prefix(sandbox["facts"]["本次检测路径"], protected)
        home, work = directory / "home", directory / "work"
        home.mkdir()
        work.mkdir()
        argv = sandbox_command(prefix, actual, home, work)
    status, output = await _probe([*argv, "--version"], directory, isolated=isolated)
    if status != "ok":
        facts["版本命令"] = {"timeout": "超时", "limit": "输出超限", "failed": "执行失败"}[status]
        state, detail = (
            "unknown",
            "版本命令未正常完成，兼容性和运行能力尚未验证。请核对原生二进制和架构。",
        )
        if status == "failed":
            state = "action"
    elif is_cli:
        match = VERSION.fullmatch(output)
        version = match[1].decode("ascii") if match else None
        facts["版本"] = (
            (version + ("（带构建或预发布标记）" if match[2] else "")) if match else "无法识别"
        )
        if version == BASELINE and not match[2]:
            state, detail = "pass", f"版本命令通过，与已测试的官方 CLI {BASELINE} 一致。"
        else:
            state, detail = (
                "unknown",
                f"兼容性未验证。已测试版本为 {BASELINE}，请另行核对 ACP 校验结果。",
            )
    else:
        facts["版本命令"] = "执行成功"
        try:
            prefix = sandbox_prefix(actual, protected)
        except OpenCodeError as exc:
            return item(name, "action", MESSAGES[exc.code], facts)
        home, work = directory / "home", directory / "work"
        home.mkdir()
        work.mkdir()
        result, _ = await _probe(sandbox_command(prefix, "/usr/bin/true", home, work), directory)
        if result == "ok":
            state, detail = "pass", "已在临时目录中完成与运行时相同的命名空间和挂载测试。"
        elif result == "failed":
            state, detail = (
                "action",
                "无法创建所需隔离。请管理员检查容器运行配置及宿主内核的 namespace 和挂载限制。",
            )
        else:
            state, detail = "unknown", "隔离能力测试未能完成。请核对部署环境后重新检查。"
        facts["隔离能力"] = {"pass": "已验证", "action": "不可用", "unknown": "未验证"}[state]
    if not configured:
        detail = (
            f"请将插件的 {field} 设为可用的绝对路径，保存并重载。当前检测使用 PATH 候选。{detail}"
        )
        state = "action"
    return item(name, state, detail, facts)


def auth_check(config, model):
    path = _path(config, "auth_path")
    if not path:
        return item("auth", "action", "请为 auth_path 填写此插件专用认证文件的绝对路径。")
    if sys.platform != "linux":
        return item("auth", "unknown", "在支持的 Linux 环境中才能校验文件所有者和权限。")
    try:
        read_api_key(path, model.partition("/")[0])
    except OpenCodeError as exc:
        detail = MESSAGES[exc.code]
        if exc.code == "not_logged_in":
            detail = "认证文件中缺少所选服务商的有效 API Key 类型条目。" + detail
        return item("auth", "action", detail)
    if not model:
        return item(
            "auth", "unknown", "文件与权限检查通过。填写有效模型 ID 后可以检查对应的服务商条目。"
        )
    return item(
        "auth", "pass", "已找到所选服务商的 API Key 条目。密钥有效性、余额和服务连通性尚未验证。"
    )


def model_check(model, invalid, config_error):
    if not model:
        return item(
            "model",
            "action",
            ("模型 ID 格式无效。" if invalid else "尚未填写模型 ID。")
            + "请将 OpenCode 列出的完整 provider/model 填入 model_id，保存并重载本插件。",
        )
    facts = {"所选模型": model}
    verified, error = runtime.cached_model_check(model)
    facts["本次加载的 ACP 记录"] = "目录已确认此 ID" if verified else "未确认"
    if config_error or error:
        reason = MESSAGES.get(config_error or error, MESSAGES["config"])
        return item(
            "model", "action", "上次加载或 ACP 校验提示：" + reason + " 处理后重载本插件。", facts
        )
    if verified:
        return item("model", "pass", "当前加载的配置已有成功的 ACP 校验记录。", facts)
    return item(
        "model",
        "unknown",
        "请保存配置并重载本插件，以 ACP 校验模型目录。重新检查环境只读取已有记录。",
        facts,
    )


class EnvironmentChecks:
    def __init__(self, config, config_error=None):
        self.config = {
            key: config[key]
            for key in (*PATH_DEFAULTS, "sandbox_enabled", "model_id")
            if key in config
        }
        self.config_error = config_error
        self.task = None
        self.cached = None
        self.checked = 0
        self.closed = False

    async def read(self, refresh=False):
        if self.closed:
            raise OpenCodeError("closed")
        if self.cached is None or time.monotonic() - self.checked >= (2 if refresh else 30):
            if not self.task or self.task.done():
                self.task = asyncio.create_task(self._collect())
            try:
                await asyncio.shield(self.task)
            except asyncio.CancelledError:
                if self.closed and not asyncio.current_task().cancelling():
                    raise OpenCodeError("closed") from None
                raise
        if self.closed:
            raise OpenCodeError("closed")
        checked_at, rows, model, invalid = self.cached
        rows = [*rows, model_check(model, invalid, self.config_error)]
        pending = next((r for r in rows if r["status"] == "action"), None)
        if pending is None:
            pending = next((r for r in rows if r["status"] == "unknown"), None)
        return {
            "checked_at": checked_at,
            "items": rows,
            "next_step": pending["detail"]
            if pending
            else "在目标会话用 /provider 选择此模型，发送第一条消息验证服务商生成。",
        }

    async def _collect(self):
        raw = self.config.get("model_id", "")
        try:
            model = explicit_model(raw.strip() if isinstance(raw, str) else raw)
        except OpenCodeError:
            model = ""
        auth_path = _path(self.config, "auth_path")
        protected = [*runtime.settings()["protected_paths"]]
        if auth_path:
            protected.append(auth_path)
        with tempfile.TemporaryDirectory(prefix="astrbot-opencode-check-") as scratch:
            directory = Path(scratch)

            async def binary(name, sandbox=None):
                location = directory / name
                location.mkdir()
                try:
                    return await binary_check(self.config, name, location, protected, sandbox)
                except (OSError, RuntimeError, ValueError):
                    return item(name, "unknown", "本地检测未完成，请核对二进制路径并重试。")

            if self.config.get("sandbox_enabled", True) is False:
                warning = "OpenCode 可访问运行用户有权访问的文件。纯聊天 Agent 和工具权限不能替代操作系统级文件隔离。"
                if _high_privilege():
                    warning += (
                        " 当前进程具有 root 或 Linux 高权限，请优先改用低权限用户或受限容器。"
                    )
                sandbox = item("sandbox", "disabled", warning)
            else:
                sandbox = await binary("sandbox")
            cli = await binary("cli", sandbox)
            reading = asyncio.create_task(asyncio.to_thread(auth_check, self.config, model))
            try:
                auth = await asyncio.shield(reading)
            except asyncio.CancelledError:
                await reading
                raise
            rows = [system_check(), cli, sandbox, auth]
        self.cached = datetime.now(timezone.utc).isoformat(), rows, model, bool(raw)
        self.checked = time.monotonic()

    async def close(self):
        self.closed = True
        if self.task is not None:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        self.task = self.cached = None
