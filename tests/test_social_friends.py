from __future__ import annotations

import contextlib
import io
import json
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from urllib.parse import parse_qs, urlsplit
import unittest
from unittest.mock import Mock, patch

from sport_sync_bridge.cli import main
from sport_sync_bridge.social_friends import NakamaFriendsClient, NakamaFriendsError


class _NakamaHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []

    def _record(self) -> None:
        body_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(body_length) if body_length else b""
        request = {
            "method": self.command,
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
            "body": json.loads(body) if body else None,
        }
        type(self).requests.append(request)

    def do_GET(self) -> None:
        self._record()
        body = json.dumps(
            {
                "friends": [
                    {"state": 1, "user": {"id": "friend-1"}},
                    {"state": 2, "user": {"id": "friend-2"}},
                ],
                "cursor": "next-page",
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:
        self._record()
        self.send_response(204)
        self.end_headers()

    def do_DELETE(self) -> None:
        self._record()
        self.send_response(204)
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        return


class NakamaFriendsClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.response = Mock(status_code=200)
        self.response.json.return_value = {"friends": [], "cursor": None}
        self.get = Mock(return_value=self.response)
        self.post = Mock(return_value=self.response)
        self.delete = Mock(return_value=self.response)
        self.client = NakamaFriendsClient(
            "https://nakama.example/api/",
            "session-token",
            get=self.get,
            post=self.post,
            delete=self.delete,
        )

    def test_list_friends_uses_aot_route_limit_cursor_and_state(self) -> None:
        result = self.client.list_friends(limit=100, cursor="next", state=2)

        self.assertEqual(result, {"friends": [], "cursor": None})
        self.get.assert_called_once_with(
            "https://nakama.example/api/v2/friend",
            params={"limit": 100, "cursor": "next", "state": 2},
            headers={
                "Authorization": "Bearer session-token",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

    def test_add_and_remove_send_one_user_id_in_ids_body(self) -> None:
        self.client.add_friend(" friend-1 ")
        self.post.assert_called_once_with(
            "https://nakama.example/api/v2/friend",
            json={"ids": ["friend-1"]},
            headers={
                "Authorization": "Bearer session-token",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

        self.response.status_code = 204
        self.client.remove_friend("friend-2")
        self.delete.assert_called_once_with(
            "https://nakama.example/api/v2/friend",
            json={"ids": ["friend-2"]},
            headers={
                "Authorization": "Bearer session-token",
                "Accept": "application/json",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

    def test_list_mutual_friend_ids_keeps_only_state_zero_users(self) -> None:
        payload = {
            "friends": [
                {"state": 0, "user": {"id": "mutual-1"}},
                {"state": 1, "user": {"id": "outgoing-1"}},
                {"state": 2, "user": {"id": "incoming-1"}},
                {"state": 3, "user": {"id": "blocked-1"}},
            ]
        }
        with patch.object(self.client, "list_friends", return_value=payload) as list_friends:
            self.assertEqual(self.client.list_mutual_friend_ids(), ["mutual-1"])
        list_friends.assert_called_once_with(limit=2000)

    def test_list_mutual_friend_ids_rejects_malformed_mutual_entries(self) -> None:
        with patch.object(
            self.client,
            "list_friends",
            return_value={"friends": [{"state": 0, "user": {}}]},
        ):
            with self.assertRaisesRegex(NakamaFriendsError, "user ID"):
                self.client.list_mutual_friend_ids()

    def test_validation_prevents_invalid_requests(self) -> None:
        invalid_calls = (
            lambda: self.client.list_friends(limit=True),
            lambda: self.client.list_friends(cursor=" "),
            lambda: self.client.list_friends(state=True),
            lambda: self.client.add_friend(" "),
            lambda: NakamaFriendsClient("https://user:pass@example.com", "token"),
            lambda: NakamaFriendsClient("https://nakama.example", " "),
        )
        for call in invalid_calls:
            with self.subTest(call=call), self.assertRaises(NakamaFriendsError):
                call()
        self.get.assert_not_called()
        self.post.assert_not_called()
        self.delete.assert_not_called()

    def test_invalid_status_and_response_payload_are_reported(self) -> None:
        self.response.status_code = 401
        with self.assertRaisesRegex(NakamaFriendsError, "HTTP 401"):
            self.client.list_friends()

        self.response.status_code = 200
        self.response.json.return_value = []
        with self.assertRaisesRegex(NakamaFriendsError, "JSON object"):
            self.client.list_friends()


class NakamaFriendsCliTests(unittest.TestCase):
    def test_cli_drives_list_add_and_remove_through_local_http(self) -> None:
        _NakamaHandler.requests = []
        server = HTTPServer(("127.0.0.1", 0), _NakamaHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        config = Mock(data_dir="/tmp/nakama-friends", log_level="INFO", log_path="/tmp/sync.log")
        outputs = []
        args_by_operation = (
            ["social", "friends", "list", "--limit", "25", "--state", "2"],
            ["social", "friends", "add", "friend-1"],
            ["social", "friends", "remove", "friend-2"],
        )
        try:
            with (
                patch.dict(
                    os.environ,
                    {
                        "GARSYNC_NAKAMA_BASE_URL": base_url,
                        "GARSYNC_NAKAMA_AUTH_TOKEN": "test-session-token",
                    },
                    clear=True,
                ),
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.ensure_directory"),
                patch("sport_sync_bridge.cli.configure_logging"),
            ):
                for args in args_by_operation:
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(main(args), 0)
                    outputs.append(json.loads(output.getvalue()))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(outputs[0]["friends"][0]["user"]["id"], "friend-1")
        self.assertEqual(outputs[1], {"operation": "add", "status": "ok", "userId": "friend-1"})
        self.assertEqual(outputs[2], {"operation": "remove", "status": "ok", "userId": "friend-2"})

        recorded = _NakamaHandler.requests
        self.assertEqual(len(recorded), 3)
        parsed_list_url = urlsplit(str(recorded[0]["path"]))
        self.assertEqual(parsed_list_url.path, "/v2/friend")
        self.assertEqual(
            parse_qs(parsed_list_url.query),
            {"limit": ["25"], "state": ["2"]},
        )
        self.assertEqual(recorded[0]["method"], "GET")
        self.assertEqual(recorded[1]["method"], "POST")
        self.assertEqual(recorded[1]["body"], {"ids": ["friend-1"]})
        self.assertEqual(recorded[2]["method"], "DELETE")
        self.assertEqual(recorded[2]["body"], {"ids": ["friend-2"]})
        self.assertTrue(
            all(request["authorization"] == "Bearer test-session-token" for request in recorded)
        )

    def test_cli_reports_missing_configuration_without_disclosing_token(self) -> None:
        config = Mock(data_dir="/tmp/nakama-friends", log_level="INFO", log_path="/tmp/sync.log")
        errors = io.StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
            patch("sport_sync_bridge.cli.ensure_directory"),
            patch("sport_sync_bridge.cli.configure_logging"),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(main(["social", "friends", "list"]), 2)
        self.assertIn("GARSYNC_NAKAMA_BASE_URL", errors.getvalue())
        self.assertNotIn("session-token", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
