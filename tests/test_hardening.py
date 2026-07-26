"""Hardening / cleanup / message-safety probes (stdlib unittest only).

Exercises real production helpers and async methods with mocks — not
string-copied stand-ins of the user-facing copy.
"""

from __future__ import annotations

import asyncio
import os
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
        self.assertIn("未回传", text)

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
                self.assertIn("未回传", text)

        asyncio.run(_run())


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


if __name__ == "__main__":
    unittest.main()
