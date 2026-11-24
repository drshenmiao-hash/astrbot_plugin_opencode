"""Run the official OpenCode executable with an isolated home and workspace."""

import asyncio
import json
import os
import secrets
import signal
import stat
from pathlib import Path
from tempfile import TemporaryDirectory

from .errors import OpenCodeError

CHAT_MODE = "astrbot-chat"
_MAX_CATALOG_BYTES = 4 * 1024 * 1024


def _copy_auth(source, target, provider):
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as auth:
            mode = os.fstat(auth.fileno()).st_mode
            if not stat.S_ISREG(mode) or mode & 0o077:
                raise OpenCodeError("auth_permissions")
            data = auth.read(262145)
        if len(data) > 262144:
            raise ValueError
        entry = json.loads(data).get(provider)
        if not isinstance(entry, dict) or entry.get("type") != "api":
            raise OpenCodeError("not_logged_in")
        key = entry.get("key")
        if not isinstance(key, str) or not key.strip():
            raise OpenCodeError("not_logged_in")
        target.parent.mkdir(parents=True, mode=0o700)
        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w") as out:
            json.dump({provider: {"type": "api", "key": key}}, out)
    except FileNotFoundError:
        raise OpenCodeError("not_logged_in") from None
    except (OSError, ValueError, AttributeError):
        raise OpenCodeError("auth_permissions") from None


class ProcessOwner:
    kind = "local"

    def __init__(self, cfg):
        self.cfg = cfg
        self.process = None
        self.scratch = None
        self._command = None
        self._close_lock = asyncio.Lock()
        self._image_support = None

    @property
    def usage_db(self):
        if self.scratch is not None:
            return Path(self.scratch.name) / "home/.local/share/opencode/opencode.db"

    async def start(self):
        cli = Path(self.cfg["cli_binary"]).resolve()
        bwrap = self.cfg["bubblewrap_binary"]
        if not cli.is_file() or not os.access(cli, os.X_OK):
            raise OpenCodeError("cli_missing")
        if not os.access(bwrap, os.X_OK):
            raise OpenCodeError("sandbox_missing")
        roots = [
            Path(name) for name in ("/usr/bin", "/usr/lib", "/lib", "/lib64") if Path(name).exists()
        ]
        system_files = [
            Path(name) for name in ("/etc/resolv.conf", "/etc/hosts", "/etc/nsswitch.conf", "/etc/ssl/certs")
            if Path(name).exists()
        ]
        for location in (*self.cfg["protected_paths"], self.cfg["auth_path"]):
            if any(Path(location).resolve().is_relative_to(path.resolve()) for path in (*roots, *system_files)):
                raise OpenCodeError("sandbox_path_conflict")
        self.scratch = TemporaryDirectory(prefix="astrbot-opencode-")
        root = Path(self.scratch.name)
        home, work = root / "home", root / "work"
        home.mkdir(mode=0o700)
        work.mkdir(mode=0o700)
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
            "HOME": "/home/opencode",
            "XDG_DATA_HOME": "/home/opencode/.local/share",
            "XDG_CONFIG_HOME": "/home/opencode/.config",
            "XDG_STATE_HOME": "/home/opencode/.local/state",
            "XDG_CACHE_HOME": "/home/opencode/.cache",
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
        argv.extend(
            [
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
            ]
        )
        # The explicit env allowlist also keeps the server password out of argv.
        argv.extend(["--", "/opt/opencode"])
        self._command = (argv, env, root)
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
                cwd=root,
                start_new_session=True,
                close_fds=True,
                limit=limit,
            )
        except OSError:
            raise OpenCodeError("transport") from None
        return self.process, {"cwd": "/work", "transport": "local"}

    async def model_supports_images(self):
        """ACP model options lack per-model capabilities; read the same CLI's catalog."""
        if self._image_support is not None:
            return self._image_support
        if self._command is None:
            raise OpenCodeError("image_capability_unverified")
        argv, env, cwd = self._command
        model = self.cfg["model"]
        provider = model.split("/", 1)[0]
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
            if process is not None and process.returncode is None:
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
        async with self._close_lock:
            process, self.process = self.process, None
            self._command = None
            self._image_support = None
            try:
                if process is not None:
                    process.stdin.close()
                    try:
                        await asyncio.wait_for(process.wait(), 2)
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
