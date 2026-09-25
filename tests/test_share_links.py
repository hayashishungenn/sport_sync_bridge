from __future__ import annotations

import contextlib
import io
import unittest

from sport_sync_bridge.cli import main
from sport_sync_bridge.share_links import build_friend_invite_url, build_group_invite_url


class ShareLinkTests(unittest.TestCase):
    def test_friend_invite_uses_compact_uuid_and_encoded_display_name(self) -> None:
        self.assertEqual(
            build_friend_invite_url("12345678-1234-abcd-5678-1234567890AB", name="小明 & Alex"),
            "https://garsync.com/i/f/?u=123456781234abcd56781234567890AB&n=%E5%B0%8F%E6%98%8E%20%26%20Alex",
        )

    def test_group_invite_includes_optional_group_and_inviter_names(self) -> None:
        self.assertEqual(
            build_group_invite_url(
                "123456781234abcd56781234567890ab",
                group_name="周末骑行",
                inviter_name="小明",
            ),
            "https://garsync.com/i/g/?g=123456781234abcd56781234567890ab"
            "&gn=%E5%91%A8%E6%9C%AB%E9%AA%91%E8%A1%8C&n=%E5%B0%8F%E6%98%8E",
        )

    def test_invalid_uuid_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "must be UUIDs"):
            build_friend_invite_url("not-a-uuid")

    def test_cli_prints_shareable_friend_url(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(
                main(["share", "friend-invite", "123456781234abcd56781234567890ab"]),
                0,
            )

        self.assertEqual(output.getvalue().strip(), "https://garsync.com/i/f/?u=123456781234abcd56781234567890ab")

    def test_cli_reports_invalid_uuid_without_traceback(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            self.assertEqual(main(["share", "friend-invite", "invalid"]), 2)

        self.assertIn("share_error=", output.getvalue())
