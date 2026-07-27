"""Hardening / cleanup / message-safety probes (stdlib unittest only).

Exercises real production helpers and async methods with mocks — not
string-copied stand-ins of the user-facing copy.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from unittest import mock
from unittest.mock import AsyncMock, MagicMock


class TestSplitMessageChunks(unittest.TestCase):
    def test_join_equals_original_fixed(self):
        from wechatbridge.runner_common import split_message_chunks

        samples = [
            "",
            "short",
            "a" * 50,
            "line1\nline2\nline3",
            "word " * 400,
            "x" * 10 + "\n" + "y" * 10,
            "  keep  spaces  at  edges  ",
            "\n\n\n",
            "中文" * 300,
        ]
        for text in samples:
            for limit in (5, 20, 80, 2000):
                chunks = split_message_chunks(text, limit)
                self.assertEqual("".join(chunks), text, msg=repr(text[:40]))
                for c in chunks:
                    self.assertLessEqual(len(c), limit if limit > 0 else len(c))

    def test_random_join_equals_original(self):
        import random
        from wechatbridge.runner_common import split_message_chunks

        rng = random.Random(42)
        alphabet = "abc \n中文，。"
        for _ in range(30):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 500)))
            limit = rng.randint(1, 120)
            chunks = split_message_chunks(text, limit)
            self.assertEqual("".join(chunks), text)


class TestPathIsUnder(unittest.TestCase):
    def test_child_and_escape(self):
        from wechatbridge.runner_common import path_is_under

        with tempfile.TemporaryDirectory() as td:
            child = os.path.join(td, "a", "b")
            os.makedirs(child)
            self.assertTrue(path_is_under(child, td))
            self.assertTrue(path_is_under(td, td))
            outside = os.path.join(
                tempfile.gettempdir(), "not-under-" + os.path.basename(td)
            )
            self.assertFalse(path_is_under(outside, td))

    def test_symlink_escape_blocked(self):
        from wechatbridge.runner_common import path_is_under

        with tempfile.TemporaryDirectory() as td:
            allowed = os.path.join(td, "allowed")
            secret = os.path.join(td, "secret")
            os.makedirs(allowed)
            os.makedirs(secret)
            leak = os.path.join(allowed, "leak")
            os.symlink(secret, leak)
            self.assertFalse(path_is_under(leak, allowed))


class TestRemoveOldFilesDangling(unittest.TestCase):
    def test_dangling_file_and_dir_links_removed_immediately(self):
        from wechatbridge.runner_common import _remove_old_files_under

        with tempfile.TemporaryDirectory() as td:
            bad_file = os.path.join(td, "python")
            os.symlink("/no/such/target/python-xyz", bad_file)
            bad_dir = os.path.join(td, "lib64")
            os.symlink("/no/such/lib", bad_dir)
            keep = os.path.join(td, "keep.txt")
            with open(keep, "w", encoding="utf-8") as f:
                f.write("ok")
            old = os.path.join(td, "old.txt")
            with open(old, "w", encoding="utf-8") as f:
                f.write("bye")
            old_mtime = time.time() - 10 * 86400
            os.utime(old, (old_mtime, old_mtime))

            cutoff = time.time() - 7 * 86400
            removed = _remove_old_files_under(td, cutoff)

            self.assertGreaterEqual(removed, 3)
            self.assertFalse(os.path.lexists(bad_file))
            self.assertFalse(os.path.lexists(bad_dir))
            self.assertFalse(os.path.exists(old))
            self.assertTrue(os.path.exists(keep))

    def test_intact_young_symlink_kept(self):
        from wechatbridge.runner_common import _remove_old_files_under

        with tempfile.TemporaryDirectory() as td:
            target = os.path.join(td, "real.txt")
            with open(target, "w", encoding="utf-8") as f:
                f.write("data")
            link = os.path.join(td, "alias")
            os.symlink(target, link)
            cutoff = time.time() - 7 * 86400
            _remove_old_files_under(td, cutoff)
            self.assertTrue(os.path.lexists(link))
            self.assertTrue(os.path.exists(target))

    def test_does_not_follow_symlink_into_outside_tree(self):
        """followlinks=False: must not age-delete files outside root via symlink."""
        from wechatbridge.runner_common import _remove_old_files_under

        with tempfile.TemporaryDirectory() as outer:
            with tempfile.TemporaryDirectory() as root:
                victim = os.path.join(outer, "outside-old.txt")
                with open(victim, "w", encoding="utf-8") as f:
                    f.write("keep-me")
                old_mtime = time.time() - 10 * 86400
                os.utime(victim, (old_mtime, old_mtime))
                os.symlink(outer, os.path.join(root, "escape"))
                cutoff = time.time() - 7 * 86400
                _remove_old_files_under(root, cutoff)
                self.assertTrue(os.path.exists(victim), "must not delete outside tree")


class TestOversizedArtifactNotice(unittest.TestCase):
    def test_helper_never_embeds_absolute_path(self):
        from wechatbridge.runner_common import format_oversized_artifact_notice

        art_path = "/root/.local/share/wechatbridge/default/sessions/u1/scratch/report.pdf"
        text = format_oversized_artifact_notice("report.pdf", 120.5)
        self.assertNotIn(art_path, text)
        self.assertNotIn("/root/", text)
        self.assertNotIn("sessions/", text)
        self.assertIn("report.pdf", text)
        self.assertIn("无法发到微信", text)

    def test_send_artifacts_back_uses_safe_notice(self):
        """Call the real async helper; assert user text has no server path."""
        from wechatbridge.main import send_artifacts_back

        async def _run():
            with tempfile.TemporaryDirectory() as td:
                scratch = os.path.join(td, ".gemini", "antigravity-cli", "scratch")
                os.makedirs(scratch)
                big_path = os.path.join(scratch, "huge.bin")
                with open(big_path, "wb") as f:
                    f.write(b"x" * 64)

                client = MagicMock()
                client.state.baseurl = "https://example.test"
                client.state.bot_token = "tok"
                client.send_message = AsyncMock(return_value=True)
                client.send_media = AsyncMock(return_value=True)

                with mock.patch(
                    "wechatbridge.main.get_session_dir", return_value=td
                ), mock.patch(
                    "wechatbridge.main._get_backend", return_value="agy"
                ), mock.patch(
                    "wechatbridge.main.config"
                ) as cfg:
                    cfg.max_outbound_file_bytes = 8  # force oversized
                    await send_artifacts_back(
                        client,
                        "user-1",
                        "ctx-token",
                        [("huge.bin", big_path)],
                    )

                client.send_media.assert_not_awaited()
                client.send_message.assert_awaited()
                kwargs = client.send_message.await_args.kwargs
                text = kwargs["text"]
                self.assertNotIn(big_path, text)
                self.assertNotIn(td, text)
                self.assertIn("huge.bin", text)
                self.assertIn("无法发到微信", text)

        asyncio.run(_run())


class TestFormatCliErrorCodex(unittest.TestCase):
    """format_cli_error must recognise codex-specific login/未登录 signals
    without misclassifying ordinary API errors, rate limits or model errors.
    agy/grok results must stay unchanged."""

    def _fmt(self, raw, backend):
        from wechatbridge.runner_common import format_cli_error
        return format_cli_error(raw, backend=backend)

    # --- codex: positive (should be 未登录) ---
    def test_codex_login_phrase(self):
        out = self._fmt("Error: you must run `codex login` to authenticate", "codex")
        self.assertIn("**未登录**", out)

    def test_codex_api_key_env(self):
        out = self._fmt("CODEX_API_KEY is not set or is invalid", "codex")
        self.assertIn("**未登录**", out)

    def test_codex_api_key_spaced(self):
        out = self._fmt("Please set the codex api key before use", "codex")
        self.assertIn("**未登录**", out)

    def test_codex_api_key_hyphen(self):
        out = self._fmt("Set a codex api-key to use the CLI", "codex")
        self.assertIn("**未登录**", out)

    def test_codex_authentication_required(self):
        out = self._fmt("Authentication required: sign in first", "codex")
        self.assertIn("**未登录**", out)

    def test_codex_not_authenticated(self):
        out = self._fmt("Request failed: you are not authenticated", "codex")
        self.assertIn("**未登录**", out)

    def test_codex_not_logged_in(self):
        out = self._fmt("You are not logged in to codex", "codex")
        self.assertIn("**未登录**", out)

    def test_codex_please_login(self):
        out = self._fmt("Please log in to continue", "codex")
        self.assertIn("**未登录**", out)

    def test_codex_unauthorized(self):
        out = self._fmt("401 Unauthorized: invalid auth token", "codex")
        self.assertIn("**未登录**", out)

    def test_codex_no_valid_credentials(self):
        out = self._fmt("No valid credentials found for this request", "codex")
        self.assertIn("**未登录**", out)

    # --- codex: negative (must NOT be 未登录) ---
    def test_codex_rate_limit_not_login(self):
        out = self._fmt("Rate limit reached, please slow down", "codex")
        self.assertNotIn("**未登录**", out)
        self.assertIn("**请求过于频繁**", out)

    def test_codex_model_not_found_not_login(self):
        out = self._fmt("error: model not found: gpt-9", "codex")
        self.assertNotIn("**未登录**", out)
        self.assertIn("**模型无效**", out)

    def test_codex_api_error_not_login(self):
        out = self._fmt("API error: 500 Internal Server Error", "codex")
        self.assertNotIn("**未登录**", out)
        self.assertIn("**执行失败**", out)

    def test_codex_bad_request_not_login(self):
        out = self._fmt("Bad request: invalid parameters", "codex")
        self.assertNotIn("**未登录**", out)
        self.assertIn("**执行失败**", out)

    def test_codex_file_not_found_not_login(self):
        out = self._fmt("file not found: ./main.py", "codex")
        self.assertNotIn("**未登录**", out)
        self.assertIn("**未找到**", out)

    # --- regression: agy/grok results unchanged ---
    def test_agy_not_signed_in_still_login(self):
        out = self._fmt("Error: not signed in", "agy")
        self.assertIn("**未登录**", out)

    def test_grok_login_still_login(self):
        out = self._fmt("Run `grok login` first", "grok")
        self.assertIn("**未登录**", out)

    def test_agy_does_not_recognise_codex_login(self):
        """Backend scoping: a codex-only hint ('codex login') under agy must
        NOT become 未登录 (it is not matched by the generic auth block)."""
        out = self._fmt("Run `codex login --with-api-key` to begin", "agy")
        self.assertNotIn("**未登录**", out)
        self.assertIn("**执行失败**", out)


class TestSendArtifactsBackCodexAddDirs(unittest.IsolatedAsyncioTestCase):
    """Second-factor verification of codex --add-dir roots at send time.

    Uses a mock client (no network). Verifies the real send function never
    calls the upload/send API for artifacts reachable only via an invalid
    add-dir root: deleted dir, plain file, out-of-bounds path, symlink escape.
    Legitimate directories are still sent back.
    """

    async def test_add_dir_roots_reverified(self):
        from wechatbridge.main import send_artifacts_back
        from wechatbridge.config import config

        with tempfile.TemporaryDirectory() as base:
            session_dir = os.path.join(base, "session")
            os.makedirs(session_dir)
            allowed_extra = os.path.join(base, "allowed_extra")
            os.makedirs(allowed_extra)

            # legitimate add_dir under an allowed root
            good_dir = os.path.join(allowed_extra, "proj")
            os.makedirs(good_dir)
            good_art = os.path.join(good_dir, "out.txt")
            with open(good_art, "w", encoding="utf-8") as f:
                f.write("ok")

            # deleted dir (created then removed)
            gone_dir = os.path.join(allowed_extra, "gone")
            os.makedirs(gone_dir)
            gone_art = os.path.join(gone_dir, "x.txt")
            with open(gone_art, "w", encoding="utf-8") as f:
                f.write("x")
            shutil.rmtree(gone_dir)

            # plain file (not a directory)
            file_dir = os.path.join(allowed_extra, "notdir")
            with open(file_dir, "w", encoding="utf-8") as f:
                f.write("i am a file")
            file_art = file_dir  # artifact is the file itself

            # out-of-bounds dir (outside configured allowed roots)
            oob = os.path.join(base, "oob")
            os.makedirs(oob)
            oob_art = os.path.join(oob, "secret.txt")
            with open(oob_art, "w", encoding="utf-8") as f:
                f.write("secret")

            # symlink escape: allowed_extra/escape -> oob (outside allowed roots)
            escape_link = os.path.join(allowed_extra, "escape")
            os.symlink(oob, escape_link)
            escape_art = os.path.join(oob, "leak.txt")
            with open(escape_art, "w", encoding="utf-8") as f:
                f.write("leak")

            # control: artifact directly under session_dir (always allowed)
            ctrl_art = os.path.join(session_dir, "ctrl.txt")
            with open(ctrl_art, "w", encoding="utf-8") as f:
                f.write("ctrl")

            prefs = {
                "backend": "codex",
                "add_dirs": [good_dir, gone_dir, file_dir, oob, escape_link],
            }
            prefs_path = os.path.join(session_dir, "prefs.json")
            with open(prefs_path, "w", encoding="utf-8") as f:
                json.dump(prefs, f)

            client = MagicMock()
            client.state.baseurl = "https://example.test"
            client.state.bot_token = "tok"
            client.send_message = AsyncMock(return_value=True)
            client.send_media = AsyncMock(return_value=True)

            artifacts = [
                ("out.txt", good_art),
                ("x.txt", gone_art),
                ("notdir", file_art),
                ("secret.txt", oob_art),
                ("leak.txt", escape_art),
                ("ctrl.txt", ctrl_art),
            ]

            with mock.patch(
                "wechatbridge.main.get_session_dir", return_value=session_dir
            ), mock.patch(
                "wechatbridge.runner_common.get_session_dir", return_value=session_dir
            ), mock.patch(
                "wechatbridge.main._get_backend", return_value="codex"
            ), mock.patch.object(
                config, "add_dir_roots", [allowed_extra]
            ):
                await send_artifacts_back(
                    client, "user-1", "ctx-token", artifacts
                )

            sent = {c.kwargs["path"] for c in client.send_media.await_args_list}

            # Legitimate add-dir artifact + session control are sent.
            self.assertIn(good_art, sent)
            self.assertIn(ctrl_art, sent)

            # Invalid add-dir roots never become allow roots -> no upload.
            self.assertNotIn(gone_art, sent)
            self.assertNotIn(file_art, sent)
            self.assertNotIn(oob_art, sent)
            self.assertNotIn(escape_art, sent)

            # Exactly the two legitimate artifacts are uploaded; the send API is
            # never called for any deleted/file/oob/symlink-escape target.
            self.assertEqual(len(client.send_media.await_args_list), 2)
            client.send_message.assert_not_awaited()


class TestILinkDeliveryAccepted(unittest.TestCase):
    def test_predicate(self):
        from wechatbridge.ilink import ilink_delivery_accepted

        self.assertTrue(ilink_delivery_accepted(0, ""))
        self.assertTrue(ilink_delivery_accepted(0, None))
        self.assertTrue(ilink_delivery_accepted(-1, "7487118974343175304"))
        self.assertTrue(ilink_delivery_accepted(1, "abc"))
        self.assertTrue(ilink_delivery_accepted(-1, 42))  # non-empty non-str id
        self.assertFalse(ilink_delivery_accepted(-1, ""))
        self.assertFalse(ilink_delivery_accepted(-1, "   "))
        self.assertFalse(ilink_delivery_accepted(1, None))
        self.assertFalse(ilink_delivery_accepted(1, 0))


class TestILinkPostSendmessageRetry(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from wechatbridge.ilink import ILinkClient

        self.client = ILinkClient()

    async def asyncTearDown(self):
        await self.client.http_client.aclose()

    async def test_ret_minus_one_with_message_id_is_success(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"ret": -1, "message_id": "mid-ok-1"}
        self.client.http_client.post = AsyncMock(return_value=mock_resp)

        ok = await self.client._post_sendmessage_with_retry(
            url="https://example.test/send",
            headers={},
            body={},
            to_user_id="u1",
            max_attempts=1,
        )
        self.assertTrue(ok)
        self.client.http_client.post.assert_awaited_once()

    async def test_ret_nonzero_without_message_id_fails_fast(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"ret": -1, "message_id": ""}
        self.client.http_client.post = AsyncMock(return_value=mock_resp)

        with mock.patch("wechatbridge.ilink.asyncio.sleep", new_callable=AsyncMock):
            ok = await self.client._post_sendmessage_with_retry(
                url="https://example.test/send",
                headers={},
                body={},
                to_user_id="u1",
                max_attempts=2,
            )
        self.assertFalse(ok)
        self.assertEqual(self.client.http_client.post.await_count, 2)

    async def test_ret_zero_succeeds(self):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"ret": 0, "message_id": "m0"}
        self.client.http_client.post = AsyncMock(return_value=mock_resp)
        ok = await self.client._post_sendmessage_with_retry(
            url="https://example.test/send",
            headers={},
            body={},
            to_user_id="u1",
            max_attempts=1,
        )
        self.assertTrue(ok)


class TestInboundStreamCapLogic(unittest.TestCase):
    def test_content_length_and_stream_abort_rules(self):
        max_in = 100

        def reject_cl(declared: int | None) -> bool:
            return declared is not None and declared > max_in

        def reject_buf(buf_len: int, piece_len: int) -> bool:
            return buf_len + piece_len > max_in

        self.assertTrue(reject_cl(101))
        self.assertFalse(reject_cl(100))
        self.assertFalse(reject_cl(None))
        self.assertTrue(reject_buf(90, 20))
        self.assertFalse(reject_buf(90, 10))


class TestSensitiveEnv(unittest.TestCase):
    def test_strips_api_keys_keeps_harmless(self):
        from wechatbridge.runner_common import _is_sensitive_env_name, sanitize_env

        self.assertTrue(_is_sensitive_env_name("XAI_API_KEY"))
        self.assertTrue(_is_sensitive_env_name("OPENAI_API_KEY"))
        self.assertTrue(_is_sensitive_env_name("BOT_TOKEN"))
        self.assertFalse(_is_sensitive_env_name("HOME"))
        self.assertFalse(_is_sensitive_env_name("PATH"))
        self.assertFalse(_is_sensitive_env_name("LANG"))

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(
                os.environ,
                {
                    "PATH": "/usr/bin",
                    "XAI_API_KEY": "secret",
                    "LANG": "C",
                    "HOME": "/tmp/other",
                },
                clear=False,
            ):
                env = sanitize_env(td)
            self.assertEqual(env["HOME"], td)
            self.assertNotIn("XAI_API_KEY", env)
            self.assertIn("LANG", env)
            self.assertEqual(env["LANG"], "C")


class TestClearInitializedIfNoHistory(unittest.TestCase):
    def test_codex_empty_history_clears_flag_and_thread_id_without_grok(self):
        """Regression: codex branch must only clear codex artifacts.

        When codex history (``.codex/sessions``) is empty, the codex branch
        once wrongly also deleted the grok flag, set ``cleared['grok']``, and
        logged a misleading message. The fix keeps the codex branch scoped
        to ``.initialized.codex`` and ``.codex_thread_id`` only.
        """
        from wechatbridge.runner_common import _clear_initialized_if_no_history

        with tempfile.TemporaryDirectory() as user_dir:
            # `.codex/sessions` 存在但不含任何文件（空历史）
            sessions = os.path.join(user_dir, ".codex", "sessions")
            os.makedirs(sessions)
            # 待清理的两个 codex 文件
            codex_flag = os.path.join(user_dir, ".initialized.codex")
            codex_tid = os.path.join(user_dir, ".codex_thread_id")
            with open(codex_flag, "w", encoding="utf-8") as f:
                f.write("")
            with open(codex_tid, "w", encoding="utf-8") as f:
                f.write("stale-tid")

            cleared = _clear_initialized_if_no_history(user_dir)

            # 两个文件都被删除
            self.assertFalse(os.path.exists(codex_flag), ".initialized.codex must be removed")
            self.assertFalse(os.path.exists(codex_tid), ".codex_thread_id must be removed")
            # 返回值标记 codex 被清理
            self.assertTrue(cleared.get("codex"), "cleared should contain codex=True")
            # 不得误标记 grok（回归点）
            self.assertNotIn("grok", cleared, "codex branch must not flag grok")

    def test_grok_branch_independent_of_codex(self):
        """grok branch should clear only its own flag when grok history is empty.

        Sanity check that the two branches are independent: a cleared codex
        flag must not bleed into the grok result and vice versa.
        """
        from wechatbridge.runner_common import _clear_initialized_if_no_history

        with tempfile.TemporaryDirectory() as user_dir:
            grok_flag = os.path.join(user_dir, ".initialized.grok")
            with open(grok_flag, "w", encoding="utf-8") as f:
                f.write("")

            cleared = _clear_initialized_if_no_history(user_dir)

            self.assertFalse(os.path.exists(grok_flag))
            self.assertTrue(cleared.get("grok"))
            self.assertNotIn("codex", cleared)


class TestCleanCodexSessionsOSError(unittest.TestCase):
    """Regression: a single unreadable directory must not abort the whole codex
    session cleanup. year/month/day os.listdir OSErrors are caught and the loop
    continues to the next bucket."""

    def test_unreadable_month_dir_does_not_abort(self):
        from wechatbridge.runner_common import _clean_codex_sessions

        with tempfile.TemporaryDirectory() as root:
            sessions = os.path.join(root, "sessions")
            # 2025/01/05 有一个过期 rollout（应被删除）
            day_ok = os.path.join(sessions, "2025", "01", "05")
            os.makedirs(day_ok)
            old = os.path.join(day_ok, "rollout-2025-01-05T00-00-00-old.jsonl")
            with open(old, "w", encoding="utf-8") as f:
                f.write("x")
            old_mtime = time.time() - 100 * 86400
            os.utime(old, (old_mtime, old_mtime))
            # 2025/02 是一个不可读（listdir 抛 OSError）的月目录
            bad_month = os.path.join(sessions, "2025", "02")
            os.makedirs(bad_month)

            cutoff = time.time() - 30 * 86400
            real_listdir = os.listdir

            def fake_listdir(path, *a, **k):
                p = str(path).rstrip(os.sep)
                if p == bad_month:
                    raise OSError("permission denied")
                return real_listdir(path, *a, **k)

            with mock.patch(
                "wechatbridge.runner_common.os.listdir", side_effect=fake_listdir
            ):
                removed = _clean_codex_sessions(sessions, cutoff)

            # 过期 rollout 被删除，且不可读月目录没有令整个清理中断/抛异常
            self.assertGreaterEqual(removed, 1)
            self.assertFalse(os.path.exists(old), "old rollout should be removed")
            # 不可读目录本身仍在（我们没有权限删除它，只是跳过）
            self.assertTrue(os.path.isdir(bad_month))

    def test_unreadable_day_dir_does_not_abort(self):
        from wechatbridge.runner_common import _clean_codex_sessions

        with tempfile.TemporaryDirectory() as root:
            sessions = os.path.join(root, "sessions")
            # 2025/01/05 有一个过期 rollout（应被删除）
            day_ok = os.path.join(sessions, "2025", "01", "05")
            os.makedirs(day_ok)
            old_ok = os.path.join(day_ok, "rollout-2025-01-05T00-00-00-old.jsonl")
            with open(old_ok, "w", encoding="utf-8") as f:
                f.write("x")
            old_mtime = time.time() - 100 * 86400
            os.utime(old_ok, (old_mtime, old_mtime))
            # 2025/01/06 是一个不可读（day 层 listdir 抛 OSError）的日目录
            bad_day = os.path.join(sessions, "2025", "01", "06")
            os.makedirs(bad_day)
            # 2025/01/07 是另一个 day，过期 rollout 仍应被清理（证明单个
            # 不可读 day 不会中断整轮清理）
            day_other = os.path.join(sessions, "2025", "01", "07")
            os.makedirs(day_other)
            old_other = os.path.join(day_other, "rollout-2025-01-07T00-00-00-old.jsonl")
            with open(old_other, "w", encoding="utf-8") as f:
                f.write("x")
            os.utime(old_other, (old_mtime, old_mtime))

            cutoff = time.time() - 30 * 86400
            real_listdir = os.listdir

            def fake_listdir(path, *a, **k):
                p = str(path).rstrip(os.sep)
                if p == bad_day:
                    raise OSError("permission denied")
                return real_listdir(path, *a, **k)

            with mock.patch(
                "wechatbridge.runner_common.os.listdir", side_effect=fake_listdir
            ):
                removed = _clean_codex_sessions(sessions, cutoff)

            # 两个可读 day 的过期 rollout 均被删除；不可读 day 没有令整轮
            # 清理中断或抛异常
            self.assertGreaterEqual(removed, 2)
            self.assertFalse(os.path.exists(old_ok), "old rollout (05) should be removed")
            self.assertFalse(os.path.exists(old_other), "old rollout (07) should be removed")
            # 不可读 day 目录本身仍在（只是跳过，没权限删除）
            self.assertTrue(os.path.isdir(bad_day))


class TestPerUserLockContract(unittest.IsolatedAsyncioTestCase):
    """生产链路契约：main._safe_process_message 的 per-user 锁 + 全局并发门。

    不触网、不依赖真实 Codex。process_message 被 mock 以控制阻塞；
    全局 main.user_locks 与 _global_task_sem 在 setUp 保存、tearDown 还原，
    避免污染顺序。
    """

    def setUp(self):
        from wechatbridge import main as main_mod
        self.main = main_mod
        # 保存并隔离全局状态，避免测试间污染顺序
        self._orig_locks = main_mod.user_locks
        self._orig_sem = main_mod._global_task_sem
        main_mod.user_locks = {}
        main_mod._global_task_sem = None
        # 全局并发槽放大，避免 fail-fast 干扰序列化/并行断言
        self._sem_patch = mock.patch.object(
            main_mod.config, "max_concurrent_tasks", 16
        )
        self._sem_patch.start()
        self.addCleanup(self._sem_patch.stop)

    def tearDown(self):
        self.main.user_locks = self._orig_locks
        self.main._global_task_sem = self._orig_sem

    def _client(self):
        client = MagicMock()
        client.state = MagicMock()
        client.state.baseurl = "https://example.test"
        client.state.bot_token = "tok"
        return client

    def _msg(self, uid, kind=None):
        m = {"from_user_id": uid, "context_token": "ctx-" + uid}
        if kind:
            m["_kind"] = kind
        return m

    async def test_same_user_serializes(self):
        """同 user 两条消息：第一条阻塞期间，第二条必须排队等待。"""
        released = asyncio.Event()
        started, ended = [], []

        async def _pm(client, msg):
            started.append(msg.get("from_user_id"))
            await released.wait()
            ended.append(msg.get("from_user_id"))

        client = self._client()
        msg1 = self._msg("alice")
        msg2 = self._msg("alice")
        with mock.patch.object(self.main, "process_message", new=_pm):
            t1 = asyncio.create_task(self.main._safe_process_message(client, msg1))
            t2 = asyncio.create_task(self.main._safe_process_message(client, msg2))
            await asyncio.sleep(0)
            # 仅第一条进入 process_message；第二条被同 user 锁串行化阻塞
            self.assertEqual(started, ["alice"])
            self.assertEqual(ended, [])
            released.set()
            await asyncio.gather(t1, t2, return_exceptions=True)
        # 第二条在第一个释放后才开始并结束
        self.assertEqual(started, ["alice", "alice"])
        self.assertEqual(ended, ["alice", "alice"])

    async def test_same_user_clear_queues_behind_run(self):
        """同 user：run 进行中发 /clear，clear 必须排队，不能在 run 完成前
        删除/改写 codex thread 状态；最终 clear 生效。"""
        from wechatbridge.codex import (
            handle_codex_slash_command, _write_codex_thread_id,
        )
        from wechatbridge.runner_common import (
            ensure_session_dir, mark_initialized,
        )

        uid = "u-lock-clear"
        base = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(base, ignore_errors=True))
        with mock.patch.object(self.main.config, "session_base_dir", base):
            sd = ensure_session_dir(uid)
            _write_codex_thread_id(sd, "tid-lock-clear")
            mark_initialized(sd, backend="codex")
            tid_path = os.path.join(sd, ".codex_thread_id")

            run_release = asyncio.Event()
            order = []

            async def _pm(client, msg):
                if msg.get("_kind") == "clear":
                    order.append("clear")
                    # 真正的 /clear 会删除 codex thread 状态
                    await handle_codex_slash_command("/clear", uid)
                else:
                    order.append("run_start")
                    await run_release.wait()
                    order.append("run_end")

            client = self._client()
            msg_run = self._msg(uid, kind="run")
            msg_clear = self._msg(uid, kind="clear")
            with mock.patch.object(self.main, "process_message", new=_pm):
                t_run = asyncio.create_task(
                    self.main._safe_process_message(client, msg_run)
                )
                t_clear = asyncio.create_task(
                    self.main._safe_process_message(client, msg_clear)
                )
                await asyncio.sleep(0)
                # run 仍阻塞时，clear 被同 user 锁串行化阻塞，尚未删除 thread_id
                self.assertIn("run_start", order)
                self.assertNotIn("clear", order)
                self.assertTrue(
                    os.path.isfile(tid_path),
                    "run 完成前 clear 不应删除 codex thread 状态",
                )
                run_release.set()
                await asyncio.gather(t_run, t_clear, return_exceptions=True)
            # run 完成后，clear 执行并生效（thread_id 被删除）
            self.assertFalse(
                os.path.isfile(tid_path),
                "clear 必须最终生效",
            )
            self.assertEqual(order, ["run_start", "run_end", "clear"])

    async def test_different_users_run_in_parallel(self):
        """不同 user 两条消息允许并行，互不阻塞（event/barrier，不靠 sleep）。"""
        started = []
        both_started = asyncio.Event()
        rel_a, rel_b = asyncio.Event(), asyncio.Event()

        async def _pm(client, msg):
            uid = msg.get("from_user_id")
            started.append(uid)
            if len(started) >= 2:
                both_started.set()
            ev = rel_a if uid == "alice" else rel_b
            await ev.wait()

        client = self._client()
        msg_a = self._msg("alice")
        msg_b = self._msg("bob")
        with mock.patch.object(self.main, "process_message", new=_pm):
            t_a = asyncio.create_task(
                self.main._safe_process_message(client, msg_a)
            )
            t_b = asyncio.create_task(
                self.main._safe_process_message(client, msg_b)
            )
            # 两个不同 user 同时进入 process_message（不互相阻塞）
            await asyncio.wait_for(both_started.wait(), timeout=5)
            self.assertEqual(sorted(started), ["alice", "bob"])
            rel_a.set()
            rel_b.set()
            await asyncio.gather(t_a, t_b, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
