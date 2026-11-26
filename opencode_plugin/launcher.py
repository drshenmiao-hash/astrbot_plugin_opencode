"""Run the official OpenCode executable with an isolated home and workspace."""

import asyncio
import json
import os
import secrets
import signal
import stat
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from .errors import OpenCodeError

CHAT_MODE = "astrbot-chat"
_MAX_CATALOG_BYTES = 4 * 1024 * 1024


def read_api_key(source, provider):
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            mode = info.st_mode
            if (
                not stat.S_ISREG(mode)
                or info.st_uid != os.geteuid()
                or mode & 0o077
                or not mode & stat.S_IRUSR
            ):
                raise OpenCodeError("auth_permissions")
            with os.fdopen(fd, "rb", closefd=False) as auth:
                data = auth.read(262145)
        finally:
            os.close(fd)
        if len(data) > 262144:
            raise ValueError
        entries = json.loads(data)
        if not isinstance(entries, dict):
            raise ValueError
        if not provider:
            return None
        entry = entries.get(provider)
        if not isinstance(entry, dict) or entry.get("type") != "api":
            raise OpenCodeError("not_logged_in")
        key = entry.get("key")
        if not isinstance(key, str) or not key.strip():
            raise OpenCodeError("not_logged_in")
        return key
    except FileNotFoundError:
        raise OpenCodeError("auth_missing") from None
    except (ValueError, RecursionError):
        raise OpenCodeError("auth_format") from None
    except OSError:
        raise OpenCodeError("auth_permissions") from None


def _copy_auth(source, target, provider):
    try:
        key = read_api_key(source, provider)
    except OpenCodeError as exc:
        # Preserve the provider's established chat error codes.
        if exc.code == "auth_missing":
            raise OpenCodeError("not_logged_in") from None
        if exc.code == "auth_format":
            raise OpenCodeError("auth_permissions") from None
        raise
    try:
        target.parent.mkdir(parents=True, mode=0o700)
        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as out:
            json.dump({provider: {"type": "api", "key": key}}, out)
    except FileNotFoundError:
        raise OpenCodeError("not_logged_in") from None
    except (OSError, ValueError, AttributeError):
        raise OpenCodeError("auth_permissions") from None


def sandbox_prefix(bwrap, protected_paths):
    roots = [
        Path(name) for name in ("/usr/bin", "/usr/lib", "/lib", "/lib64") if Path(name).exists()
    ]
    system_files = [
        Path(name)
        for name in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/ssl/certs")
        if Path(name).exists()
    ]
    for location in protected_paths:
        if any(
            Path(location).resolve().is_relative_to(path.resolve())
            for path in (*roots, *system_files)
        ):
            raise OpenCodeError("sandbox_path_conflict")
    argv = [
        bwrap,
        "--unshare-all",
        "--share-net",
        "--die-with-parent",
        "--new-session",
        "--cap-drop",
        "ALL",
    ]
    for path in roots:
        argv.extend(["--ro-bind", str(path), str(path)])
    argv.extend(
        ["--symlink", "usr/bin", "/bin", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp"]
    )
    for path in system_files:
        argv.extend(["--ro-bind", str(path), str(path)])
    return argv


def sandbox_command(prefix, cli, home, work):
    return [
        *prefix,
        "--ro-bind",
        str(cli),
        "/opt/opencode",
        "--bind",
        str(home),
        "/home/opencode",
        "--bind",
        str(work),
        "/work",
        "--chdir",
        "/work",
        "--",
        "/opt/opencode",
    ]


class ProcessOwner:
    kind = "local"

    def __init__(self, cfg):
        self.cfg = cfg
        self.process = None
        self.scratch = None
        self._command = None
        self._close_task = None
        self._image_support = None

    @property
    def usage_db(self):
        if self.scratch is not None:
            return Path(self.scratch.name) / "home/.local/share/opencode/opencode.db"

    async def start(self):
        if sys.platform != "linux":
            raise OpenCodeError("linux_required")
        cli = Path(self.cfg["cli_binary"]).resolve()
        if not cli.is_file() or not os.access(cli, os.X_OK):
            raise OpenCodeError("cli_missing")
        prefix = None
        if self.cfg["sandbox_enabled"]:
            bwrap = self.cfg["bubblewrap_binary"]
            if not Path(bwrap).is_file() or not os.access(bwrap, os.X_OK):
                raise OpenCodeError("sandbox_missing")
            prefix = sandbox_prefix(bwrap, (*self.cfg["protected_paths"], self.cfg["auth_path"]))
        self.scratch = TemporaryDirectory(prefix="astrbot-opencode-")
        root = Path(self.scratch.name)
        home, work = root / "home", root / "work"
        home.mkdir(mode=0o700)
        work.mkdir(mode=0o700)
        if prefix is not None:
            argv = sandbox_command(prefix, cli, home, work)
            cli_home, cli_work = Path("/home/opencode"), Path("/work")
            cwd = root
        else:
            argv = [str(cli)]
            cli_home, cli_work = home, work
            cwd = work
        provider = self.cfg["model"].split("/", 1)[0]
        _copy_auth(self.cfg["auth_path"], home / ".local/share/opencode/auth.json", provider)
        # Only this primary agent is selectable. The global and per-agent
        # permission denials also apply if the model emits a tool attempt.
        config = {
            "model": self.cfg["model"],
            "enabled_providers": [provider],
            "default_agent": CHAT_MODE,
            "permission": {"*": "deny"},
            "agent": {
                CHAT_MODE: {
                    "mode": "primary",
                    "prompt": "You are a conversation assistant. Reply with text. Never use tools.",
                    "permission": {"*": "deny"},
                },
                "build": {"disable": True},
                "plan": {"disable": True},
                # OpenCode would otherwise make a second model call to title
                # the conversation. AstrBot owns the conversation lifecycle.
                "title": {"disable": True},
            },
            "autoupdate": False,
            "share": "disabled",
            "mcp": {},
        }
        env = {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "HOME": str(cli_home),
            "XDG_DATA_HOME": str(cli_home / ".local/share"),
            "XDG_CONFIG_HOME": str(cli_home / ".config"),
            "XDG_STATE_HOME": str(cli_home / ".local/state"),
            "XDG_CACHE_HOME": str(cli_home / ".cache"),
            "OPENCODE_CONFIG_CONTENT": json.dumps(config),
            "OPENCODE_SERVER_PASSWORD": secrets.token_urlsafe(32),
            **dict.fromkeys(
                (
                    "OPENCODE_DISABLE_AUTOUPDATE",
                    "OPENCODE_DISABLE_MODELS_FETCH",
                    "OPENCODE_DISABLE_PROJECT_CONFIG",
                    "OPENCODE_DISABLE_DEFAULT_PLUGINS",
                    "OPENCODE_DISABLE_CLAUDE_CODE",
                    "OPENCODE_DISABLE_EXTERNAL_SKILLS",
                    "OPENCODE_DISABLE_EMBEDDED_WEB_UI",
                    "OPENCODE_DISABLE_LSP_DOWNLOAD",
                    "OPENCODE_DISABLE_AUTOCOMPACT",
                    "OPENCODE_DISABLE_PRUNE",
                ),
                "1",
            ),
        }
        if prefix is None:
            private_tmp = root / "tmp"
            private_tmp.mkdir(mode=0o700)
            env.update(
                TMPDIR=str(private_tmp),
                GIT_CONFIG_NOSYSTEM="1",
                GIT_CEILING_DIRECTORIES=str(root),
                # OpenCode 1.19.6 merges managed settings after inline config.
                # Keep that lookup inside this dedicated instance as in bubblewrap.
                OPENCODE_TEST_MANAGED_CONFIG_DIR=str(home / ".managed"),
            )
        self._command = (argv, env, cwd)
        try:
            limit = max(self.cfg["context_max_bytes"] * 2, self.cfg["output_max_bytes"]) + 65536
            self.process = await asyncio.create_subprocess_exec(
                *argv,
                "acp",
                "--hostname",
                "127.0.0.1",
                "--port",
                "0",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=cwd,
                start_new_session=True,
                close_fds=True,
                limit=limit,
            )
        except OSError:
            raise OpenCodeError("transport") from None
        return self.process, {"cwd": str(cli_work), "transport": "local"}

    async def model_supports_images(self):
        """ACP model options lack per-model capabilities; read the same CLI's catalog."""
        if self._image_support is not None:
            return self._image_support
        if self._command is None:
            raise OpenCodeError("image_capability_unverified")
        argv, env, cwd = self._command
        model = self.cfg["model"]
        provider, model_id = model.split("/", 1)
        # The CLI can exit with large verbose catalogs still buffered on stdout.
        # Its whitelist filters built-in models without declaring a new model.
        config = json.loads(env["OPENCODE_CONFIG_CONTENT"])
        config["provider"] = {provider: {"whitelist": [model_id]}}
        env = {**env, "OPENCODE_CONFIG_CONTENT": json.dumps(config)}
        process = None
        try:
            async with asyncio.timeout(5):
                process = await asyncio.create_subprocess_exec(
                    *argv,
                    "models",
                    provider,
                    "--verbose",
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.DEVNULL,
                    env=env,
                    cwd=cwd,
                    start_new_session=True,
                    close_fds=True,
                )
                output = bytearray()
                while chunk := await process.stdout.read(65536):
                    output.extend(chunk)
                    if len(output) > _MAX_CATALOG_BYTES:
                        raise OpenCodeError("image_capability_unverified")
                if await process.wait() != 0:
                    raise OpenCodeError("image_capability_unverified")
                _, separator, entry = ("\n" + output.decode()).partition(f"\n{model}\n")
                if not separator:
                    raise OpenCodeError("image_capability_unverified")
                details, _ = json.JSONDecoder().raw_decode(entry.lstrip())
                image = details["capabilities"]["input"]["image"]
                if type(image) is not bool:
                    raise OpenCodeError("image_capability_unverified")
        except (OSError, ValueError, TypeError, KeyError, TimeoutError):
            raise OpenCodeError("image_capability_unverified") from None
        finally:
            if process is not None and (
                process.returncode is None or not self.cfg["sandbox_enabled"]
            ):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                # A full asyncio reader can otherwise keep process.wait() blocked.
                process.stdout._transport.close()
                try:
                    await asyncio.wait_for(process.wait(), 2)
                except asyncio.TimeoutError:
                    raise OpenCodeError("cleanup_timeout") from None
        self._image_support = image
        return image

    async def close(self):
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_process())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            await self._close_task
            raise

    async def _close_process(self):
        process, self.process = self.process, None
        self._command = None
        self._image_support = None
        try:
            if process is not None:
                process.stdin.close()
                try:
                    await asyncio.wait_for(process.wait(), 2)
                    if not self.cfg["sandbox_enabled"]:
                        # Direct children lack bubblewrap's PID namespace teardown.
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                except asyncio.TimeoutError:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    for stream in (process.stdout, process.stderr):
                        transport = getattr(stream, "_transport", None)
                        if transport is not None:
                            transport.close()
                    try:
                        await asyncio.wait_for(process.wait(), 2)
                    except asyncio.TimeoutError:
                        raise OpenCodeError("cleanup_timeout") from None
        finally:
            if self.scratch is not None:
                self.scratch.cleanup()
                self.scratch = None
