"""Runtime invariants shared by the default sandbox and explicit direct mode."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from opencode_plugin.settings import settings_from


class LaunchModesTests(unittest.IsolatedAsyncioTestCase):
    async def test_run_paths_permission_denials_credentials_and_teardown(self):
        from opencode_plugin.launcher import CHAT_MODE, ProcessOwner

        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(
                os.environ,
                {
                    "OTHER_API_KEY": "production-sentinel",
                    "OPENCODE_CONFIG_CONTENT": "production-sentinel",
                    "XDG_CONFIG_HOME": "/production-sentinel",
                    "HTTPS_PROXY": "production-sentinel",
                },
            ),
        ):
            auth = Path(tmp) / "auth.json"
            auth.write_text(
                json.dumps(
                    {
                        "fixture": {"type": "api", "key": "selected-key", "extra": "drop"},
                        "other": {"type": "api", "key": "production-sentinel"},
                    }
                )
            )
            auth.chmod(0o600)
            for enabled in (True, False):
                cfg = settings_from(
                    {
                        "model_id": "fixture/test",
                        "auth_path": str(auth),
                        "cli_binary": "/usr/bin/true",
                        "bubblewrap_binary": "/usr/bin/true" if enabled else "",
                        "sandbox_enabled": enabled,
                    }
                )
                owner = ProcessOwner(cfg)
                self.addAsyncCleanup(owner.close)
                proc, ready = await owner.start()
                root = Path(owner.scratch.name)
                argv, envp, cwd = owner._command
                home = root / "home"
                copy = home / ".local/share/opencode/auth.json"
                self.assertEqual(
                    json.loads(copy.read_text()),
                    {"fixture": {"type": "api", "key": "selected-key"}},
                )
                self.assertEqual(copy.stat().st_mode & 0o777, 0o600)
                self.assertNotIn("production-sentinel", str((argv, envp)))
                self.assertNotIn("selected-key", str((argv, envp)))
                self.assertNotIn("HTTPS_PROXY", envp)
                conf = json.loads(envp["OPENCODE_CONFIG_CONTENT"])
                self.assertEqual(conf["enabled_providers"], ["fixture"])
                self.assertEqual(conf["permission"], {"*": "deny"})
                self.assertEqual(conf["agent"][CHAT_MODE]["permission"], {"*": "deny"})
                self.assertEqual(conf["default_agent"], CHAT_MODE)
                self.assertEqual(envp["OPENCODE_DISABLE_PROJECT_CONFIG"], "1")
                if enabled:
                    self.assertIn("--unshare-all", argv)
                    self.assertEqual(ready["cwd"], "/work")
                else:
                    self.assertEqual(argv, ["/usr/bin/true"])
                    self.assertEqual(cwd, root / "work")
                    self.assertEqual(ready["cwd"], str(cwd))
                    for key in (
                        "HOME",
                        "TMPDIR",
                        "XDG_DATA_HOME",
                        "XDG_CONFIG_HOME",
                        "XDG_STATE_HOME",
                        "XDG_CACHE_HOME",
                        "OPENCODE_TEST_MANAGED_CONFIG_DIR",
                    ):
                        self.assertTrue(Path(envp[key]).is_relative_to(root))
                await owner.close()
                self.assertIsNotNone(proc.returncode)
                self.assertFalse(root.exists())
