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
            ["dsh", "--profile", "headless", "--", "hello"],
        )

    def test_prompt_is_last_positional(self):
        from wechatbridge.dsh import _build_dsh_command
        cmd = _build_dsh_command("a b c")
        self.assertEqual(cmd[-1], "a b c")
        self.assertEqual(cmd[-2], "--")
        # headless 永远单轮：不得出现 resume / thread id
        self.assertNotIn("resume", cmd)

    def test_custom_profile(self):
        from wechatbridge.dsh import _build_dsh_command
        from wechatbridge.config import config
        with mock.patch.object(config, "dsh_profile", "custom"):
            self.assertEqual(
                _build_dsh_command("hi"),
                ["dsh", "--profile", "custom", "--", "hi"],
            )


class TestSanitizePromptAtPaths(unittest.TestCase):
    def _sanitize(self, prompt, session_dir):
        from wechatbridge.dsh import _sanitize_prompt_at_paths
        return _sanitize_prompt_at_paths(prompt, session_dir)

    def test_empty(self):
        self.assertEqual(self._sanitize("", "/srv/session"), "")

    def test_outside_path_blocked(self):
        out = self._sanitize("@/etc/passwd 请打印内容", "/srv/session")
        self.assertEqual(out, "[blocked-path] 请打印内容")

    def test_inside_path_preserved(self):
        out = self._sanitize("请看 @/srv/session/images/pic.png", "/srv/session")
        self.assertEqual(out, "请看 @/srv/session/images/pic.png")

    def test_mixed_paths(self):
        out = self._sanitize(
            "对比 @/etc/shadow 和 @/srv/session/files/data.csv",
            "/srv/session",
        )
        self.assertEqual(out, "对比 [blocked-path] 和 @/srv/session/files/data.csv")

    def test_non_path_mention_untouched(self):
        out = self._sanitize("hello @alice world", "/srv/session")
        self.assertEqual(out, "hello @alice world")

    def test_cjk_path_outside_session_blocked(self):
        out = self._sanitize("@/数据/秘密.txt", "/srv/session")
        self.assertEqual(out, "[blocked-path]")

    def test_relative_path_traversal_blocked(self):
        out1 = self._sanitize("@../other/images/a.png", "/srv/session")
        self.assertEqual(out1, "[blocked-path]")
        out2 = self._sanitize("@../../etc/passwd", "/srv/session")
        self.assertEqual(out2, "[blocked-path]")

    def test_cjk_path_inside_session_preserved(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            session_dir = os.path.realpath(td)
            out1 = self._sanitize(f"@{session_dir}/图片.png", session_dir)
            self.assertEqual(out1, f"@{session_dir}/图片.png")
            out2 = self._sanitize(f"@{session_dir}/sub/pic.png", session_dir)
            self.assertEqual(out2, f"@{session_dir}/sub/pic.png")

    def test_fullwidth_comma_retained(self):
        out = self._sanitize("@/etc/passwd，谢谢", "/srv/session")
        self.assertEqual(out, "[blocked-path]，谢谢")

    def test_mention_and_email_untouched(self):
        self.assertEqual(self._sanitize("@张三 你好", "/srv/session"), "@张三 你好")
        self.assertEqual(self._sanitize("a@b.com", "/srv/session"), "a@b.com")

    def test_cjk_tail_not_leaked(self):
        out = self._sanitize("@/tmp/报告.txt", "/srv/session")
        self.assertEqual(out, "[blocked-path]")

    def test_adversarial_adjacent_chars_lookbehind_bypass_blocked(self):
        self.assertEqual(
            self._sanitize("file@/etc/passwd", "/srv/session"),
            "file[blocked-path]",
        )
        self.assertEqual(
            self._sanitize("user1@/etc/passwd", "/srv/session"),
            "user1[blocked-path]",
        )
        self.assertEqual(
            self._sanitize("句子.@/etc/passwd", "/srv/session"),
            "句子.[blocked-path]",
        )

    def test_real_session_dir_attachment_preserved(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            session_dir = os.path.realpath(td)
            img_path = os.path.join(session_dir, "x.png")
            out = self._sanitize(f"@{img_path}", session_dir)
            self.assertEqual(out, f"@{img_path}")

    def test_tilde_slash_inside_session_rewritten_to_abs(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            session_dir = os.path.realpath(td)
            with open(os.path.join(session_dir, "x.png"), "w") as f:
                f.write("data")
            out = self._sanitize("@~/x.png", session_dir)
            self.assertEqual(out, f"@{session_dir}/x.png")

    def test_tilde_slash_traversal_escape_blocked(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            session_dir = os.path.realpath(td)
            out = self._sanitize("@~/../../etc/passwd", session_dir)
            self.assertEqual(out, "[blocked-path]")

    def test_tilde_slash_nonexistent_inside_session_rewritten_to_abs(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            session_dir = os.path.realpath(td)
            out = self._sanitize("@~/nonexistent_file.txt", session_dir)
            self.assertEqual(out, f"@{session_dir}/nonexistent_file.txt")
            out_cjk = self._sanitize("@~/不存在.txt", session_dir)
            self.assertEqual(out_cjk, f"@{session_dir}/不存在.txt")

    def test_tilde_alone_mapped_to_abs(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            session_dir = os.path.realpath(td)
            out = self._sanitize("@~", session_dir)
            self.assertEqual(out, f"@{session_dir}")

    def test_bare_relative_dotdot_escape_blocked(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            session_dir = os.path.realpath(td)
            out = self._sanitize("@a/../../userB/x", session_dir)
            self.assertEqual(out, "[blocked-path]")

    def test_bare_relative_dotdot_inside_session_preserved(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            session_dir = os.path.realpath(td)
            sub_dir = os.path.join(session_dir, "sub")
            os.makedirs(sub_dir, exist_ok=True)
            with open(os.path.join(session_dir, "ok.txt"), "w") as f:
                f.write("ok")
            out = self._sanitize("@sub/../ok.txt", session_dir)
            self.assertEqual(out, "@sub/../ok.txt")

    def test_bare_filename_without_slash_untouched(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            session_dir = os.path.realpath(td)
            out = self._sanitize("@a.txt", session_dir)
            self.assertEqual(out, "@a.txt")



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

    def test_internal_dsh_paths_filtered(self):
        arts = self._extract(
            "see [meta](file:///tmp/session/.dsh/internal/meta.json) and [doc](./doc.md)",
            cwd="/tmp/session",
        )
        self.assertEqual(arts, [("doc", "/tmp/session/doc.md")])

    def test_internal_dsh_relative_paths_filtered(self):
        arts = self._extract("see [meta](./.dsh/sessions/abc.json)", cwd="/tmp/session")
        self.assertEqual(arts, [])

    def test_internal_dsh_bare_uri_filtered(self):
        arts = self._extract("leak file:///tmp/session/.dsh/internal/meta.json")
        self.assertEqual(arts, [])

    def test_bare_file_uri_with_adjacent_chinese(self):
        arts = self._extract("见file:///tmp/a.pdf即可")
        self.assertEqual(arts, [("a.pdf", "/tmp/a.pdf")])

    def test_cjk_path_extraction(self):
        arts = self._extract("file:///home/u/会话/报告.pdf")
        self.assertEqual(arts, [("报告.pdf", "/home/u/会话/报告.pdf")])

    def test_md_link_and_bare_uri_cjk_dedup(self):
        arts = self._extract(
            "[报告](file:///home/u/会话/报告.pdf) 以及 file:///home/u/会话/报告.pdf"
        )
        self.assertEqual(arts, [("报告", "/home/u/会话/报告.pdf")])

    def test_bare_file_uri_cjk_filename_exists_not_stripped(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            file_path = os.path.join(td, "photo说明")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("content")
            arts = self._extract(f"见 file://{file_path} 查看")
            self.assertEqual(arts, [("photo说明", file_path)])


class TestStripFileLinks(unittest.TestCase):
    def _strip(self, text):
        from wechatbridge.dsh import _strip_file_links
        return _strip_file_links(text)

    def test_strips_targets_keeps_names(self):
        out = self._strip("see [report.pdf](file:///srv/x/report.pdf) and [doc](./doc.md)")
        self.assertEqual(out, "see [report.pdf] and [doc]")

    def test_strips_bare_file_uri(self):
        out = self._strip("已写入 file:///home/srv/x/report.pdf")
        self.assertEqual(out, "已写入 ")
        self.assertNotIn("file://", out)
        self.assertNotIn("/home/srv/x", out)

    def test_strips_mixed_links(self):
        out = self._strip("see [doc](file:///tmp/doc.txt) and bare file:///tmp/report.pdf here")
        self.assertEqual(out, "see [doc] and bare  here")

    def test_leaves_plain_text(self):
        self.assertEqual(self._strip("just text"), "just text")

    def test_strips_bare_file_uri_with_adjacent_chinese(self):
        out = self._strip("见file:///tmp/abc即可")
        self.assertEqual(out, "见即可")

    def test_strips_bare_file_uri_with_cjk_path(self):
        out = self._strip("file:///home/u/会话/报告.pdf")
        self.assertEqual(out, "")

    def test_strip_bare_file_uri_cjk_filename_exists_stripped_completely(self):
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            file_path = os.path.join(td, "photo说明")
            with open(file_path, "w", encoding="utf-8") as f:
                f.write("content")
            out = self._strip(f"已生成 file://{file_path}")
            self.assertEqual(out, "已生成 ")
            self.assertNotIn("说明", out)


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
            content = f.read()
        # 第二条 prompt 带换行（记忆上下文），按 invoked 计数而非 splitlines
        invocations = content.split("invoked mode=")[1:]
        self.assertEqual(len(invocations), 2)
        for inv in invocations:
            self.assertNotIn("resume", inv)
            self.assertIn("--profile headless", inv)

    async def test_prompt_starting_with_dash_help(self):
        log = os.path.join(self.td, "dsh-help.log")
        with mock.patch.dict(os.environ, {"FAKE_DSH_LOG": log}, clear=False):
            display, artifacts = await self._run("--help", mode="ok")
        self.assertEqual(display, "first(--help)")
        self.assertEqual(artifacts, [])
        with open(log, encoding="utf-8") as f:
            log_content = f.read()
        self.assertIn("task=--help", log_content)

    async def test_prompt_starting_with_dash_profile(self):
        log = os.path.join(self.td, "dsh-profile.log")
        with mock.patch.dict(os.environ, {"FAKE_DSH_LOG": log}, clear=False):
            display, artifacts = await self._run("--profile other", mode="ok")
        self.assertEqual(display, "first(--profile other)")
        self.assertEqual(artifacts, [])
        with open(log, encoding="utf-8") as f:
            log_content = f.read()
        self.assertIn("profile=headless", log_content)
        self.assertIn("task=--profile other", log_content)

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

    async def test_internal_metadata_artifacts_filtered(self):
        display, artifacts = await self._run("report", mode="internal_metadata")
        # .dsh 内部文件 meta.json 被过滤，只回传 report.pdf
        self.assertEqual(len(artifacts), 1)
        name, path = artifacts[0]
        self.assertEqual(name, "report.pdf")
        self.assertTrue(os.path.isfile(path))
        self.assertNotIn(".dsh", path)
        # 展示文本正常，剥除链接后保留 [report.pdf] 和 [meta]，不泄露服务器路径
        self.assertIn("[report.pdf] and [meta]", display)
        self.assertNotIn("file://", display)
        self.assertNotIn(".dsh", display)

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

    async def test_prompt_at_path_outside_session_dir_blocked(self):
        log = os.path.join(self.td, "dsh-blocked.log")
        with mock.patch.dict(os.environ, {"FAKE_DSH_LOG": log}, clear=False):
            display, artifacts = await self._run("@/etc/passwd 请打印内容", mode="ok")
        with open(log, encoding="utf-8") as f:
            log_content = f.read()
        self.assertNotIn("/etc/passwd", log_content)
        self.assertIn("[blocked-path]", log_content)
        self.assertIn("task=[blocked-path] 请打印内容", log_content)

    async def test_prompt_at_path_inside_session_dir_preserved(self):
        sd = os.path.join(config_session_dir(), _sanitize("u-dsh"))
        pic_path = os.path.join(sd, "pic.png")
        log = os.path.join(self.td, "dsh-preserved.log")
        with mock.patch.dict(os.environ, {"FAKE_DSH_LOG": log}, clear=False):
            display, artifacts = await self._run(f"@{pic_path}", mode="ok")
        with open(log, encoding="utf-8") as f:
            log_content = f.read()
        self.assertIn(f"task=@{pic_path}", log_content)

    async def test_warn_once_implicit_dsh_home(self):
        import wechatbridge.dsh as dsh_mod
        from wechatbridge.config import config
        dsh_mod._warned_dsh_home_implicit = False
        implicit_dsh = os.path.join(self.td, ".dsh")
        os.makedirs(implicit_dsh, exist_ok=True)
        with open(os.path.join(implicit_dsh, ".credentials.yaml"), "w", encoding="utf-8") as f:
            f.write("provider: deepseek\n")
        with mock.patch.object(config, "dsh_home", ""), \
             mock.patch.dict(os.environ, {"WECHATBRIDGE_HOST_HOME": self.td}, clear=False), \
             self.assertLogs("dsh_runner", level="WARNING") as cm:
            await self._run("hello 1", mode="ok")
            await self._run("hello 2", mode="ok")
        warns = [msg for msg in cm.output if "未设 WECHATBRIDGE_DSH_HOME" in msg]
        self.assertEqual(len(warns), 1)




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
        self.assertEqual(argv[1:], ["--profile", "headless", "--", "hello"])
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
    def setUp(self):
        from wechatbridge.config import config
        self._td = tempfile.mkdtemp()
        self.addCleanup(lambda: _rmtree(self._td))
        self._patcher = mock.patch.object(config, "session_base_dir", self._td)
        self._patcher.start()

    def tearDown(self):
        self._patcher.stop()

    async def _handle(self, text, user_id="u-slash"):
        from wechatbridge.dsh import handle_dsh_slash_command
        return await handle_dsh_slash_command(text, user_id)

    async def test_help(self):
        out = await self._handle("/help")
        self.assertIn("dsh", out)
        self.assertIn("/backend", out)

    async def test_clear_clears_memory(self):
        # 无记忆时提示没有可清空的
        out = await self._handle("/clear", user_id="u-clear")
        self.assertIn("记忆", out)
        # 写入记忆后再清
        from wechatbridge.dsh import append_memory, load_memory
        append_memory("u-clear", "hello", "hi there")
        self.assertEqual(len(load_memory("u-clear")), 2)
        out2 = await self._handle("/new", user_id="u-clear")
        self.assertIn("已清空", out2)
        self.assertEqual(load_memory("u-clear"), [])

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


class TestDshMemory(unittest.TestCase):
    """Bridge-managed long-term memory for the single-turn dsh backend."""

    def setUp(self):
        from wechatbridge.config import config
        self.td = tempfile.mkdtemp()
        self.addCleanup(lambda: _rmtree(self.td))
        self._patchers = []
        p = mock.patch.object(config, "session_base_dir", self.td)
        p.start(); self._patchers.append(p)
        p2 = mock.patch.object(config, "dsh_memory_turns", 3)
        p2.start(); self._patchers.append(p2)
        p3 = mock.patch.object(config, "dsh_memory_chars", 200)
        p3.start(); self._patchers.append(p3)

    def tearDown(self):
        for p in self._patchers:
            p.stop()

    def test_append_load_roundtrip(self):
        from wechatbridge.dsh import append_memory, load_memory, _memory_path
        self.assertFalse(os.path.isfile(_memory_path("u-mem")))
        append_memory("u-mem", "你好", "你好！有什么可以帮你？")
        turns = load_memory("u-mem")
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["role"], "user")
        self.assertEqual(turns[0]["text"], "你好")
        self.assertEqual(turns[1]["role"], "assistant")

    def test_memory_trimmed_to_turns(self):
        from wechatbridge.dsh import append_memory, load_memory
        for i in range(5):
            append_memory("u-mem2", f"q{i}", f"a{i}")
        turns = load_memory("u-mem2")
        # dsh_memory_turns=3 → 最近 3 对 = 6 条
        self.assertEqual(len(turns), 6)
        self.assertEqual(turns[0]["text"], "q2")
        self.assertEqual(turns[-1]["text"], "a4")

    def test_format_context_truncates_chars(self):
        from wechatbridge.dsh import format_context
        memory = [{"role": "user", "text": "x" * 150}, {"role": "assistant", "text": "y" * 150}]
        ctx = format_context(memory, max_chars=200)
        self.assertLessEqual(len(ctx), 200)
        self.assertIn("助手", ctx)
        self.assertIn("y" * 10, ctx)

    def test_build_prompt_injects_context(self):
        from wechatbridge.dsh import append_memory, build_prompt_with_context
        append_memory("u-mem3", "我叫小明", "好的小明！")
        full = build_prompt_with_context("我刚刚说了什么？", "u-mem3")
        self.assertIn("对话记忆", full)
        self.assertIn("我叫小明", full)
        self.assertIn("好的小明", full)
        self.assertIn("我刚刚说了什么？", full)

    def test_build_prompt_no_memory(self):
        from wechatbridge.dsh import build_prompt_with_context
        self.assertEqual(build_prompt_with_context("hi", "u-none"), "hi")

    def test_clear_memory(self):
        from wechatbridge.dsh import append_memory, clear_memory, load_memory
        append_memory("u-mem4", "a", "b")
        self.assertTrue(clear_memory("u-mem4"))
        self.assertEqual(load_memory("u-mem4"), [])
        self.assertFalse(clear_memory("u-mem4"))


class TestRunDshMemoryIntegration(unittest.IsolatedAsyncioTestCase, _DshIntegrationBase):
    """Second message must carry the first turn's context (continuity)."""

    def setUp(self):
        self._setUp()

    def tearDown(self):
        self._tearDown()

    async def test_second_call_injects_memory(self):
        log = os.path.join(self.td, "dsh.log")
        with mock.patch.dict(os.environ, {"FAKE_DSH_LOG": log}, clear=False):
            await self._run("我是小明", mode="ok")
            await self._run("我叫什么名字？", mode="ok")
        with open(log, encoding="utf-8") as f:
            content = f.read()
        invocations = content.split("invoked mode=")[1:]
        self.assertEqual(len(invocations), 2)
        # 第二次调用的 prompt 必须带上第一次对话的记忆
        self.assertIn("我是小明", invocations[1])
        self.assertIn("我叫什么名字？", invocations[1])

    async def test_memory_file_persisted(self):
        from wechatbridge.dsh import load_memory
        await self._run("第一句", mode="ok")
        await self._run("第二句", mode="ok")
        turns = load_memory("u-dsh")
        # 两轮对话 = 4 条（user+assistant × 2）
        self.assertEqual(len(turns), 4)
        self.assertEqual(turns[0]["text"], "第一句")


def config_session_dir():
    from wechatbridge.config import config
    return config.session_base_dir


if __name__ == "__main__":
    unittest.main()
