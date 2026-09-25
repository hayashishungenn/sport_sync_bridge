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
from sport_sync_bridge.social_feed import SocialFeedClient, SocialFeedError


class _FeedHandler(BaseHTTPRequestHandler):
    last_request: dict[str, object] = {}

    def do_GET(self) -> None:
        type(self).last_request = {
            "path": self.path,
            "authorization": self.headers.get("Authorization"),
        }
        body = json.dumps({"items": [{"id": "local-activity"}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class _FollowFeedHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []

    def do_GET(self) -> None:
        parsed = urlsplit(self.path)
        type(self).requests.append(
            {
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
            }
        )
        if parsed.path == "/nakama/v2/friend":
            payload = {
                "friends": [
                    {"state": 0, "user": {"id": "mutual-user"}},
                    {"state": 1, "user": {"id": "outgoing-user"}},
                    {"state": 2, "user": {"id": "incoming-user"}},
                    {"state": 3, "user": {"id": "blocked-user"}},
                ]
            }
        elif parsed.path == "/social/feed/follow":
            payload = {"items": [{"id": "followed-activity"}]}
        else:
            self.send_error(404)
            return

        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return


class _FeedMutationHandler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []

    def _record(self) -> None:
        body_length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(body_length) if body_length else b""
        type(self).requests.append(
            {
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": json.loads(body) if body else None,
            }
        )

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


class SocialFeedClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.response = Mock(status_code=200)
        self.response.json.return_value = {"items": [{"id": "activity-1"}]}
        self.get = Mock(return_value=self.response)
        self.post = Mock(return_value=self.response)
        self.delete = Mock(return_value=self.response)
        self.client = SocialFeedClient(
            "https://social.example/api/",
            "session-token",
            get=self.get,
            post=self.post,
            delete=self.delete,
        )

    def test_newest_feed_uses_aot_route_pagination_and_bearer_session(self) -> None:
        result = self.client.get_feed("newest", page=2, limit=15)

        self.assertEqual(result, {"items": [{"id": "activity-1"}]})
        self.get.assert_called_once_with(
            "https://social.example/api/feed/newest",
            params={"page": 2, "limit": 15},
            headers={
                "Authorization": "Bearer session-token",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

    def test_user_and_nearby_feeds_include_required_query_fields(self) -> None:
        self.client.get_feed("user", user_id="user-1")
        self.assertEqual(
            self.get.call_args.kwargs["params"],
            {"page": 0, "limit": 40, "userId": "user-1"},
        )

        self.client.get_feed("nearby", latitude=31.2, longitude=121.5)
        self.assertEqual(
            self.get.call_args.kwargs["params"],
            {"page": 0, "limit": 40, "lat": 31.2, "lng": 121.5},
        )

    def test_follow_feed_accepts_explicit_following_ids(self) -> None:
        self.client.get_feed("follow", following_ids=["u1", "u2"])
        self.assertEqual(
            self.get.call_args.kwargs["params"],
            {"page": 0, "limit": 40, "userIds": "u1,u2"},
        )

    def test_invalid_feed_arguments_are_rejected_before_request(self) -> None:
        cases = [
            ("user", {}),
            ("nearby", {"latitude": 100, "longitude": 0}),
            ("newest", {"user_id": "not-applicable"}),
            ("newest", {"following_ids": ["u1"]}),
            ("newest", {"page": True}),
            ("newest", {"limit": 0}),
            ("follow", {"following_ids": "u1"}),
        ]
        for kind, kwargs in cases:
            with self.subTest(kind=kind, kwargs=kwargs), self.assertRaises(SocialFeedError):
                self.client.get_feed(kind, **kwargs)
        self.get.assert_not_called()

    def test_non_200_and_non_object_responses_are_rejected(self) -> None:
        self.response.status_code = 401
        with self.assertRaisesRegex(SocialFeedError, "HTTP 401"):
            self.client.get_feed("hot")

        self.response.status_code = 200
        self.response.json.return_value = []
        with self.assertRaisesRegex(SocialFeedError, "JSON object"):
            self.client.get_feed("hot")

    def test_thumb_and_unthumb_use_aot_route_and_activity_id_body(self) -> None:
        self.client.thumb(" activity-1 ")
        self.post.assert_called_once_with(
            "https://social.example/api/thumb",
            json={"activityId": "activity-1"},
            headers={
                "Authorization": "Bearer session-token",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

        self.response.status_code = 204
        self.client.unthumb("activity-2")
        self.delete.assert_called_once_with(
            "https://social.example/api/thumb",
            json={"activityId": "activity-2"},
            headers={
                "Authorization": "Bearer session-token",
                "Content-Type": "application/json",
            },
            timeout=30,
        )

    def test_thumb_rejects_empty_activity_id_and_non_success_status(self) -> None:
        with self.assertRaisesRegex(SocialFeedError, "activity ID"):
            self.client.thumb(" ")
        self.post.assert_not_called()

        self.response.status_code = 403
        with self.assertRaisesRegex(SocialFeedError, "HTTP 403"):
            self.client.thumb("activity-1")

    def test_base_url_and_token_are_validated_without_echoing_token(self) -> None:
        with self.assertRaises(SocialFeedError):
            SocialFeedClient("https://user:password@example.com", "session-token")
        with self.assertRaises(SocialFeedError) as error:
            SocialFeedClient("https://social.example", " ")
        self.assertNotIn("session-token", str(error.exception))


class SocialFeedCliTests(unittest.TestCase):
    def test_cli_performs_local_http_request_and_prints_response(self) -> None:
        _FeedHandler.last_request = {}
        server = HTTPServer(("127.0.0.1", 0), _FeedHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        config = Mock(data_dir="/tmp/social-feed", log_level="INFO", log_path="/tmp/sync.log")
        output = io.StringIO()
        try:
            with (
                patch.dict(os.environ, {"CODEX_SOCIAL_FEED_TEST_TOKEN": "test-only-token"}),
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.ensure_directory"),
                patch("sport_sync_bridge.cli.configure_logging"),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(
                    main(
                        [
                            "social",
                            "feed",
                            "hot",
                            "--base-url",
                            f"http://127.0.0.1:{server.server_port}/api",
                            "--token-env",
                            "CODEX_SOCIAL_FEED_TEST_TOKEN",
                        ]
                    ),
                    0,
                )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        result = json.loads(output.getvalue())
        self.assertEqual(result, {"items": [{"id": "local-activity"}]})
        request = _FeedHandler.last_request
        parsed = urlsplit(str(request["path"]))
        self.assertEqual(parsed.path, "/api/feed/hot")
        self.assertEqual(parse_qs(parsed.query), {"page": ["0"], "limit": ["40"]})
        self.assertEqual(request["authorization"], "Bearer test-only-token")

    def test_follow_feed_fetches_only_mutual_nakama_friend_ids(self) -> None:
        _FollowFeedHandler.requests = []
        server = HTTPServer(("127.0.0.1", 0), _FollowFeedHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}"
        config = Mock(data_dir="/tmp/social-follow", log_level="INFO", log_path="/tmp/sync.log")
        output = io.StringIO()
        try:
            with (
                patch.dict(
                    os.environ,
                    {
                        "GARSYNC_SOCIAL_BASE_URL": f"{base_url}/social",
                        "GARSYNC_NAKAMA_BASE_URL": f"{base_url}/nakama",
                        "GARSYNC_NAKAMA_AUTH_TOKEN": "test-session-token",
                    },
                    clear=True,
                ),
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.ensure_directory"),
                patch("sport_sync_bridge.cli.configure_logging"),
                contextlib.redirect_stdout(output),
            ):
                self.assertEqual(main(["social", "feed", "follow"]), 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(
            json.loads(output.getvalue()),
            {"items": [{"id": "followed-activity"}]},
        )
        requests = _FollowFeedHandler.requests
        self.assertEqual(len(requests), 2)
        friends_url = urlsplit(str(requests[0]["path"]))
        self.assertEqual(friends_url.path, "/nakama/v2/friend")
        self.assertEqual(parse_qs(friends_url.query), {"limit": ["2000"]})
        feed_url = urlsplit(str(requests[1]["path"]))
        self.assertEqual(feed_url.path, "/social/feed/follow")
        self.assertEqual(
            parse_qs(feed_url.query),
            {"page": ["0"], "limit": ["40"], "userIds": ["mutual-user"]},
        )
        self.assertTrue(
            all(request["authorization"] == "Bearer test-session-token" for request in requests)
        )

    def test_follow_feed_requires_nakama_url_when_ids_are_not_overridden(self) -> None:
        config = Mock(data_dir="/tmp/social-follow", log_level="INFO", log_path="/tmp/sync.log")
        errors = io.StringIO()
        with (
            patch.dict(
                os.environ,
                {
                    "GARSYNC_SOCIAL_BASE_URL": "https://social.example",
                    "GARSYNC_NAKAMA_AUTH_TOKEN": "test-session-token",
                },
                clear=True,
            ),
            patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
            patch("sport_sync_bridge.cli.ensure_directory"),
            patch("sport_sync_bridge.cli.configure_logging"),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(main(["social", "feed", "follow"]), 2)
        self.assertIn("GARSYNC_NAKAMA_BASE_URL", errors.getvalue())

    def test_cli_drives_thumb_and_unthumb_through_local_http(self) -> None:
        _FeedMutationHandler.requests = []
        server = HTTPServer(("127.0.0.1", 0), _FeedMutationHandler)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base_url = f"http://127.0.0.1:{server.server_port}/social"
        config = Mock(data_dir="/tmp/social-thumb", log_level="INFO", log_path="/tmp/sync.log")
        outputs = []
        commands = (
            ["social", "thumb", "activity-1"],
            ["social", "unthumb", "activity-2"],
        )
        try:
            with (
                patch.dict(
                    os.environ,
                    {
                        "GARSYNC_SOCIAL_BASE_URL": base_url,
                        "GARSYNC_NAKAMA_AUTH_TOKEN": "test-session-token",
                    },
                    clear=True,
                ),
                patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                patch("sport_sync_bridge.cli.ensure_directory"),
                patch("sport_sync_bridge.cli.configure_logging"),
            ):
                for command in commands:
                    output = io.StringIO()
                    with contextlib.redirect_stdout(output):
                        self.assertEqual(main(command), 0)
                    outputs.append(json.loads(output.getvalue()))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(
            outputs,
            [
                {"operation": "thumb", "status": "ok", "activityId": "activity-1"},
                {"operation": "unthumb", "status": "ok", "activityId": "activity-2"},
            ],
        )
        recorded = _FeedMutationHandler.requests
        self.assertEqual(len(recorded), 2)
        self.assertEqual(
            [(request["method"], request["path"], request["body"]) for request in recorded],
            [
                ("POST", "/social/thumb", {"activityId": "activity-1"}),
                ("DELETE", "/social/thumb", {"activityId": "activity-2"}),
            ],
        )
        self.assertTrue(
            all(request["authorization"] == "Bearer test-session-token" for request in recorded)
        )

    def test_cli_uses_environment_token_and_prints_json(self) -> None:
        output = io.StringIO()
        config = Mock(data_dir="/tmp/social-feed", log_level="INFO", log_path="/tmp/sync.log")
        with (
            patch.dict(
                os.environ,
                {
                    "GARSYNC_SOCIAL_BASE_URL": "https://social.example/api",
                    "GARSYNC_NAKAMA_AUTH_TOKEN": "session-token",
                },
                clear=False,
            ),
            patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
            patch("sport_sync_bridge.cli.ensure_directory"),
            patch("sport_sync_bridge.cli.configure_logging"),
            patch("sport_sync_bridge.cli.SocialFeedClient.get_feed", return_value={"items": []}) as fetch,
            contextlib.redirect_stdout(output),
        ):
            self.assertEqual(main(["social", "feed", "newest"]), 0)

        result = json.loads(output.getvalue())
        self.assertEqual(result, {"items": []})
        fetch.assert_called_once_with(
            "newest",
            page=0,
            limit=40,
            user_id=None,
            latitude=None,
            longitude=None,
            following_ids=None,
        )

    def test_cli_requires_configured_base_url_and_token_without_revealing_values(self) -> None:
        config = Mock(data_dir="/tmp/social-feed", log_level="INFO", log_path="/tmp/sync.log")
        errors = io.StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
            patch("sport_sync_bridge.cli.ensure_directory"),
            patch("sport_sync_bridge.cli.configure_logging"),
            contextlib.redirect_stderr(errors),
        ):
            self.assertEqual(main(["social", "feed", "newest"]), 2)
        self.assertIn("GARSYNC_SOCIAL_BASE_URL", errors.getvalue())
        self.assertNotIn("session-token", errors.getvalue())


if __name__ == "__main__":
    unittest.main()
