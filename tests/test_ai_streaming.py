from __future__ import annotations

import json
import contextlib
import io
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace

from sport_sync_bridge.activity_analysis import request_ai_analysis
from sport_sync_bridge.activity_library import LocalActivityLibrary
from sport_sync_bridge.cli import main
from sport_sync_bridge.state import StateDB
from tests.activity_fixtures import create_gpx


class FakeResponse:
    def __init__(self, lines: list[str | bytes], *, status_code: int = 200) -> None:
        self.lines = lines
        self.status_code = status_code
        self.ok = status_code < 400
        self.closed = False

    def iter_lines(self, *, decode_unicode: bool = False) -> list[str | bytes]:
        return self.lines

    def json(self) -> dict[str, object]:
        return json.loads("\n".join(str(line) for line in self.lines))

    def close(self) -> None:
        self.closed = True


class StreamingAIHandler(BaseHTTPRequestHandler):
    last_request: dict[str, object] | None = None

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        type(self).last_request = json.loads(self.rfile.read(length))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.end_headers()
        for content in ("流式", "分析结果"):
            event = json.dumps({"choices": [{"delta": {"content": content}}]}, ensure_ascii=False)
            self.wfile.write(f"data: {event}\n\n".encode("utf-8"))
            self.wfile.flush()
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()

    def log_message(self, _format: str, *_args: object) -> None:
        return None


class AIStreamingTests(unittest.TestCase):
    def test_streaming_chat_completion_emits_sse_deltas_and_closes_response(self) -> None:
        response = FakeResponse(
            [
                b": keep-alive",
                b"data: {\"choices\":[{\"delta\":{\"role\":\"assistant\"}}]}",
                b"",
                b"data: {\"choices\":[{\"delta\":{\"content\":\"Hello\"}}]}",
                b"",
                b"data: {\"choices\":[{\"delta\":{\"content\":\" world\"}}]}",
                b"",
                b"data: [DONE]",
                b"",
            ]
        )
        emitted: list[str] = []

        with patch("requests.post", return_value=response) as post:
            result = request_ai_analysis(
                base_url="https://ai.example/v1/",
                model="test-model",
                api_key="secret",
                prompt="analyze",
                on_delta=emitted.append,
            )

        self.assertEqual(result, "Hello world")
        self.assertEqual(emitted, ["Hello", " world"])
        self.assertTrue(response.closed)
        self.assertEqual(post.call_args.args[0], "https://ai.example/v1/chat/completions")
        self.assertTrue(post.call_args.kwargs["stream"])
        self.assertTrue(post.call_args.kwargs["json"]["stream"])
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer secret")

    def test_json_response_is_supported_when_server_does_not_stream(self) -> None:
        payload = {"choices": [{"message": {"content": " JSON result "}}]}
        response = FakeResponse([json.dumps(payload)])
        emitted: list[str] = []

        with patch("requests.post", return_value=response):
            result = request_ai_analysis(
                base_url="https://ai.example/v1/chat/completions",
                model="test-model",
                api_key=None,
                prompt="analyze",
                on_delta=emitted.append,
            )

        self.assertEqual(result, "JSON result")
        self.assertEqual(emitted, [" JSON result "])
        self.assertTrue(response.closed)

    def test_invalid_streaming_event_is_rejected(self) -> None:
        response = FakeResponse(["data: not-json", ""])

        with patch("requests.post", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "invalid streaming event"):
                request_ai_analysis(
                    base_url="https://ai.example/v1",
                    model="test-model",
                    api_key=None,
                    prompt="analyze",
                )

        self.assertTrue(response.closed)

    def test_http_error_closes_response_and_does_not_expose_response_body(self) -> None:
        response = FakeResponse(["private server response"], status_code=401)

        with patch("requests.post", return_value=response):
            with self.assertRaisesRegex(RuntimeError, "HTTP 401") as raised:
                request_ai_analysis(
                    base_url="https://ai.example/v1",
                    model="test-model",
                    api_key="secret",
                    prompt="analyze",
                )

        self.assertNotIn("secret", str(raised.exception))
        self.assertTrue(response.closed)

    def test_ai_analysis_cli_displays_local_http_sse_and_saves_full_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = StateDB(root / "state.db")
            try:
                activity_path = create_gpx(root / "activity.gpx")
                library = LocalActivityLibrary(state, root / ".data")
                imported = library.import_paths([activity_path])[0]
                server = ThreadingHTTPServer(("127.0.0.1", 0), StreamingAIHandler)
                server_thread = threading.Thread(target=server.serve_forever, daemon=True)
                server_thread.start()
                try:
                    config = SimpleNamespace(
                        data_dir=root / ".data",
                        db_path=state.path,
                        log_level="INFO",
                        log_path=root / "sync.log",
                        ai_api_base_url=f"http://127.0.0.1:{server.server_port}/v1",
                        ai_model="local-test-model",
                        ai_api_key=None,
                    )
                    output = io.StringIO()
                    with (
                        patch("sport_sync_bridge.cli.AppConfig.load", return_value=config),
                        patch("sport_sync_bridge.cli.configure_logging"),
                        contextlib.redirect_stdout(output),
                    ):
                        status = main(["ai-analysis", imported.fingerprint])
                    self.assertEqual(status, 0)
                    self.assertEqual(output.getvalue(), "流式分析结果\n")
                    self.assertIsNotNone(StreamingAIHandler.last_request)
                    self.assertTrue(StreamingAIHandler.last_request["stream"])
                    saved = state.list_ai_analysis_results(imported.fingerprint)
                    self.assertEqual(len(saved), 1)
                    self.assertEqual(saved[0]["content"], "流式分析结果")
                finally:
                    server.shutdown()
                    server.server_close()
                    server_thread.join(timeout=2)
            finally:
                state.close()


if __name__ == "__main__":
    unittest.main()
