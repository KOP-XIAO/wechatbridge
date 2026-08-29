"""Unit + integration tests for wechatbridge.dsh (DeepSeek Harness backend)."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_FAKE_DSH = os.path.join(_HERE, "fake_dsh.py")


def _rmtree(path):
    try:
        shutil.rmtree(path)
    except OSError:
        pass


def _sanitize(user_id):
    from wechatbridge.runner_common import sanitize_user_id
    return sanitize_user_id(user_id)


class _FakeProc:
    """Minimal stand-in for an asyncio subprocess used by spawn-capture tests."""

    def __init__(self, stdout="", stderr="", rc=0, pid=9999):
        self._so = stdout.encode("utf-8")
        self._se = stderr.encode("utf-8")
        self.returncode = rc
        self.pid = pid

    async def communicate(self):
        return self._so, self._se


class TestBuildDshCommand(unittest.TestCase):
    def setUp(self):
        from wechatbridge.config import config
        self._patchers = []
        p = mock.patch.object(config, "dsh_binary_path", "dsh")
        p.start()
        self._patchers.append(p)
        p2 = mock.patch.object(config, "dsh_profile", "headless")
        p2.start()
        self._patchers.append(p2)

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def test_basic(self):
        from wechatbridge.dsh import _build_dsh_command
        self.assertEqual(
            _build_dsh_command("hello"),
            ["dsh", "--profile", "headless", "hello"],
        )

    def test_prompt_is_last_positional(self):
        from wechatbridge.dsh import _build_dsh_command
        cmd = _build_dsh_command("a b c")
        self.assertEqual(cmd[-1], "a b c")
        # headless 永远单轮：不得出现 resume / thread id
        self.assertNotIn("resume", cmd)

    def test_custom_profile(self):
        from wechatbridge.dsh import _build_dsh_command
        from wechatbridge.config import config
        with mock.patch.object(config, "dsh_profile", "custom"):
            self.assertEqual(
                _build_dsh_command("hi"),
                ["dsh", "--profile", "custom", "hi"],
            )


class TestExtractArtifacts(unittest.TestCase):
    def _extract(self, text, cwd=""):
        from wechatbridge.dsh import extract_artifacts
        return extract_artifacts(text, cwd=cwd)

    def test_empty(self):
        self.assertEqual(self._extract(""), [])
        self.assertEqual(self._extract("no links here"), [])

    def test_file_uri_link(self):
        arts = self._extract("see [report.pdf](file:///tmp/x/report.pdf)")
        self.assertEqual(arts, [("report.pdf", "/tmp/x/report.pdf")])

    def test_bare_file_uri(self):
        arts = self._extract("wrote file:///tmp/x/out.md")
        self.assertEqual(arts, [("out.md", "/tmp/x/out.md")])

    def test_relative_link_resolves_against_cwd(self):
        arts = self._extract("see [doc.md](./doc.md)", cwd="/srv/ws")
        self.assertEqual(arts, [("doc.md", "/srv/ws/doc.md")])

    def test_parent_relative_link(self):
        arts = self._extract("see [conf](../conf.yaml)", cwd="/srv/ws/sub")
        self.assertEqual(arts, [("conf", "/srv/ws/conf.yaml")])

    def test_absolute_link(self):
        arts = self._extract("see [out](/tmp/out.txt)")
        self.assertEqual(arts, [("out", "/tmp/out.txt")])

    def test_http_and_bare_names_ignored(self):
        arts = self._extract(
            "see [site](https://example.com) and [tool](grep) and [x](file:///ok.txt)"
        )
        self.assertEqual(arts, [("x", "/ok.txt")])

    def test_dedup(self):
        arts = self._extract(
            "a [x](file:///tmp/a.txt) b [x](file:///tmp/a.txt)"
        )
        self.assertEqual(len(arts), 1)

    def test_urlencoded_paths(self):
        arts = self._extract("see [报 告](file:///tmp/my%20report.pdf)")
        self.assertEqual(arts, [("报 告", "/tmp/my report.pdf")])


class TestStripFileLinks(unittest.TestCase):
    def _strip(self, text):
        from wechatbridge.dsh import _strip_file_links
        return _strip_file_links(text)

    def test_strips_targets_keeps_names(self):
        out = self._strip("see [report.pdf](file:///srv/x/report.pdf) and [doc](./doc.md)")
        self.assertEqual(out, "see [report.pdf] and [doc]")

    def test_leaves_plain_text(self):
        self.assertEqual(self._strip("just text"), "just text")


class _DshIntegrationBase:
    """Shared setup: temp session dir + fake host DSH_HOME with credentials."""

    def _setUp(self):
        self.td = tempfile.mkdtemp()
        self.addCleanup(lambda: _rmtree(self.td))
        self.host_home = os.path.join(self.td, "host-dsh")
        os.makedirs(self.host_home, exist_ok=True)
        cred = os.path.join(self.host_home, ".credentials.yaml")
        with open(cred, "w", encoding="utf-8") as f:
            f.write("provider: deepseek\n")

        from wechatbridge.config import config
        self._patchers = []
        p = mock.patch.object(config, "session_base_dir", os.path.join(self.td, "sessions"))
        p.start(); self._patchers.append(p)
        # 写一个 shim，subprocess 跑 shim 时 exec 到 python fake_dsh.py
        self.shim = os.path.join(self.td, "dsh-shim.py")
        with open(self.shim, "w", encoding="utf-8") as f:
            f.write(
                "#!/usr/bin/env python3\n"
                "import sys, os\n"
                f"sys.argv[0] = {_FAKE_DSH!r}\n"
                "sys.argv.insert(0, sys.executable)\n"
                "os.execv(sys.executable, sys.argv)\n"
            )
        os.chmod(self.shim, 0o755)
        p2 = mock.patch.object(config, "dsh_binary_path", self.shim)
        p2.start(); self._patchers.append(p2)
        p3 = mock.patch.object(config, "dsh_home", self.host_home)
        p3.start(); self._patchers.append(p3)
        p4 = mock.patch.object(config, "dsh_timeout", 30)
        p4.start(); self._patchers.append(p4)

    def _tearDown(self):
        for p in self._patchers:
            p.stop()

    async def _run(self, prompt, user_id="u-dsh", mode="ok", timeout=None):
        from wechatbridge import dsh as dsh_mod
        with mock.patch.dict(os.environ, {"FAKE_DSH_MODE": mode}, clear=False):
            return await dsh_mod.run_dsh(prompt, user_id, timeout=timeout)


class TestRunDshIntegration(unittest.IsolatedAsyncioTestCase, _DshIntegrationBase):
    def setUp(self):
        self._setUp()

    def tearDown(self):
        self._tearDown()

    async def test_ok_reply_and_first_flag(self):
        display, artifacts = await self._run("hello", mode="ok")
        self.assertEqual(display, "first(hello)")
        self.assertEqual(artifacts, [])
        sd = os.path.join(config_session_dir(), _sanitize("u-dsh"))
        # 首条成功会打 .initialized.dsh 标记
        self.assertTrue(os.path.exists(os.path.join(sd, ".initialized.dsh")))

    async def test_second_call_does_not_resume(self):
        log = os.path.join(self.td, "dsh.log")
        with mock.patch.dict(os.environ, {"FAKE_DSH_LOG": log}, clear=False):
            await self._run("one", mode="ok")
            await self._run("two", mode="ok")
        with open(log, encoding="utf-8") as f:
            lines = f.read().strip().splitlines()
        self.assertEqual(len(lines), 2)
        for line in lines:
            self.assertNotIn("resume", line)
            self.assertIn("--profile headless", line)

    async def test_artifact_file_uri(self):
        display, artifacts = await self._run("make pdf", mode="artifact_link")
        self.assertEqual(len(artifacts), 1)
        name, path = artifacts[0]
        self.assertEqual(name, "result.txt")
        self.assertTrue(os.path.isfile(path))
        sd = os.path.join(config_session_dir(), _sanitize("u-dsh"))
        # macOS /var -> /private/var 是符号链接，两端都 realpath 再比前缀
        self.assertTrue(os.path.realpath(path).startswith(os.path.realpath(sd)))
        # 显示文本里的 file:/// 链接目标被剥掉
        self.assertIn("[result.txt]", display)
        self.assertNotIn("file://", display)

    async def test_artifact_relative_link(self):
        display, artifacts = await self._run("doc", mode="artifact_relative")
        self.assertEqual(len(artifacts), 1)
        name, path = artifacts[0]
        self.assertEqual(name, "doc.md")
        sd = os.path.join(config_session_dir(), _sanitize("u-dsh"))
        self.assertTrue(
            os.path.realpath(path).startswith(os.path.realpath(sd))
            and path.endswith("doc.md")
        )
        self.assertNotIn("./doc.md", display)

    async def test_empty_reply(self):
        display, artifacts = await self._run("hi", mode="empty")
        self.assertEqual(display, "（这次没有文字回复）")
        self.assertEqual(artifacts, [])

    async def test_nonzero_exit_maps_to_error_bubble(self):
        display, artifacts = await self._run("boom", mode="fail")
        self.assertEqual(artifacts, [])
        self.assertIn("❌", display)
        # 首条失败不得打 initialized
        sd = os.path.join(config_session_dir(), _sanitize("u-dsh"))
        self.assertFalse(os.path.exists(os.path.join(sd, ".initialized.dsh")))

    async def test_dsh_error_maps_to_generic_failure(self):
        display, _ = await self._run("overloaded", mode="dsh_error")
        self.assertIn("❌", display)
        self.assertIn("执行失败", display)

    async def test_not_logged_in_maps_to_auth_bubble(self):
        display, _ = await self._run("hi", mode="not_logged_in")
        self.assertIn("未登录", display)

    async def test_missing_credentials_preflight(self):
        # 删除宿主凭据 → 预检直接返回未登录，不拉起子进程
        cred = os.path.join(self.host_home, ".credentials.yaml")
        os.remove(cred)
        display, artifacts = await self._run("hi", mode="ok")
        self.assertEqual(artifacts, [])
        self.assertIn("未登录", display)

    async def test_timeout_returns_friendly_error(self):
        display, _ = await self._run("slow", mode="timeout", timeout=0.3)
        self.assertIn("超时", display)

    async def test_oversized_prompt_rejected(self):
        display, artifacts = await self._run("x" * (130 * 1024), mode="ok")
        self.assertEqual(artifacts, [])
        self.assertIn("消息过长", display)


class TestDshSpawnEnv(unittest.IsolatedAsyncioTestCase, _DshIntegrationBase):
    """DSH_HOME 显式传给子进程、HOME=session_dir、剥离 DSH_SESSION_* 变量。"""

    def setUp(self):
        self._setUp()

    def tearDown(self):
        self._tearDown()

    async def test_env_and_cwd(self):
        captured = []

        async def spawn(*args, **kwargs):
            captured.append((list(args), kwargs))
            return _FakeProc("ok\n", "", 0)

        from wechatbridge import dsh as dsh_mod
        with mock.patch.dict(
            os.environ,
            {"DSH_SESSION_ID": "leak", "DSH_SESSION_JSONL": "/leak/session.jsonl", "DSH_SHELL": "1"},
            clear=False,
        ), mock.patch("asyncio.create_subprocess_exec", side_effect=spawn):
            display, _ = await dsh_mod.run_dsh("hello", "u-env")

        self.assertEqual(display, "ok")
        self.assertEqual(len(captured), 1)
        argv, kwargs = captured[0]
        self.assertEqual(argv[0], self.shim)
        self.assertEqual(argv[1:], ["--profile", "headless", "hello"])
        sd = os.path.join(config_session_dir(), _sanitize("u-env"))
        self.assertEqual(kwargs["cwd"], sd)
        env = kwargs["env"]
        self.assertEqual(env["DSH_HOME"], self.host_home)
        self.assertEqual(env["HOME"], sd)
        self.assertNotIn("DSH_SESSION_ID", env)
        self.assertNotIn("DSH_SESSION_JSONL", env)
        self.assertNotIn("DSH_SHELL", env)
        self.assertEqual(env["PAGER"], "cat")
        self.assertEqual(env["CI"], "true")


class TestDshSlashCommands(unittest.IsolatedAsyncioTestCase):
    async def _handle(self, text, user_id="u-slash"):
        from wechatbridge.dsh import handle_dsh_slash_command
        return await handle_dsh_slash_command(text, user_id)

    async def test_help(self):
        out = await self._handle("/help")
        self.assertIn("dsh", out)
        self.assertIn("/backend", out)

    async def test_clear_is_single_turn_notice(self):
        out = await self._handle("/clear")
        self.assertIn("单轮", out)
        out2 = await self._handle("/new")
        self.assertIn("单轮", out2)

    async def test_model_commands_not_supported(self):
        for cmd in ("/model gemini-3", "/models", "/fast", "/planning", "/persona hi", "/add-dir /tmp", "/agents"):
            out = await self._handle(cmd)
            self.assertIn("不支持", out, cmd)

    async def test_dangerous_rejected(self):
        for cmd in ("/exit", "/quit", "/logout"):
            out = await self._handle(cmd)
            self.assertIn("禁用", out)

    async def test_tui_panels_not_supported(self):
        out = await self._handle("/config")
        self.assertIn("微信端不支持", out)

    async def test_passthrough_returns_none(self):
        self.assertIsNone(await self._handle("普通消息"))
        self.assertIsNone(await self._handle("/whatever-custom"))


class TestDshBackendRegistration(unittest.TestCase):
    def test_known_backends_includes_dsh(self):
        from wechatbridge.runner_common import KNOWN_BACKENDS
        self.assertIn("dsh", KNOWN_BACKENDS)

    def test_switch_backend_prefs_to_dsh(self):
        from wechatbridge.runner_common import (
            KNOWN_BACKENDS, default_prefs, switch_backend_prefs,
        )
        prefs = default_prefs()
        prefs["backend"] = "agy"
        old, new = switch_backend_prefs(prefs, "dsh")
        self.assertEqual((old, new), ("agy", "dsh"))
        self.assertEqual(prefs["backend"], "dsh")
        self.assertIn("dsh", prefs["by_backend"])

    def test_default_prefs_has_dsh_slot(self):
        from wechatbridge.runner_common import default_prefs
        prefs = default_prefs()
        self.assertIn("dsh", prefs["by_backend"])


def config_session_dir():
    from wechatbridge.config import config
    return config.session_base_dir


if __name__ == "__main__":
    unittest.main()
