from __future__ import annotations

import json
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .activity_library import LocalActivityLibrary, MAX_FILE_BYTES
from .state import StateDB


_UPLOAD_PAGE = """<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>运动文件传输</title><style>
body{font:16px system-ui,sans-serif;background:#f2f4f8;margin:0;min-height:100vh;display:grid;place-items:center;color:#172033}
main{background:white;max-width:32rem;width:calc(100% - 3rem);padding:2rem;border-radius:1rem;box-shadow:0 12px 40px #17203318}
input,button{font:inherit}input{display:block;width:100%;margin:1rem 0}button{border:0;border-radius:.6rem;background:#1769e0;color:white;padding:.8rem 1.2rem;cursor:pointer}
#status{min-height:1.5rem;overflow-wrap:anywhere}
</style><main><h1>运动文件传输</h1><p>选择 FIT、GPX、TCX、ZIP、JSON 或轨迹 CSV 文件导入本地活动库。</p>
<form id="upload"><input name="file" type="file" required accept=".fit,.gpx,.tcx,.zip,.json,.csv"><button>上传并导入</button></form>
<p id="status" role="status"></p><script>
document.getElementById('upload').addEventListener('submit',async e=>{e.preventDefault();const status=document.getElementById('status');
status.textContent='正在上传…';try{const response=await fetch('/upload',{method:'POST',body:new FormData(e.currentTarget)});
const body=await response.json();if(!response.ok)throw new Error(body.error||'上传失败');
status.textContent=`已导入 ${body.imported} 个活动文件${body.duplicates?`，重复 ${body.duplicates} 个`:''}`;
}catch(error){status.textContent=error.message||'上传失败';}});
</script></main></html>"""


def serve_transfer(host: str, port: int, library: LocalActivityLibrary, max_bytes: int = MAX_FILE_BYTES) -> None:
    handler_type = _make_handler(library, max_bytes)
    server = ThreadingHTTPServer((host, port), handler_type)
    server.daemon_threads = True
    try:
        print(f"transfer=http://{server.server_address[0]}:{server.server_address[1]}/")
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def _make_handler(library: LocalActivityLibrary, max_bytes: int) -> type[BaseHTTPRequestHandler]:
    class TransferHandler(BaseHTTPRequestHandler):
        server_version = "SportSyncBridgeTransfer/1.0"

        def do_GET(self) -> None:
            if urlsplit(self.path).path == "/health":
                self._send(200, b'{"status":"ok"}', "application/json; charset=utf-8")
                return
            if urlsplit(self.path).path != "/":
                self._send(404, b"Not found", "text/plain; charset=utf-8")
                return
            self._send(200, _UPLOAD_PAGE.encode("utf-8"), "text/html; charset=utf-8")

        def do_POST(self) -> None:
            if urlsplit(self.path).path != "/upload":
                self._send(404, b"Not found", "text/plain; charset=utf-8")
                return
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self._send_error(411, "A valid Content-Length header is required")
                return
            if length <= 0 or length > max_bytes + 1024 * 1024:
                self._send_error(413, "Upload size is outside the allowed limit")
                return
            content_type = self.headers.get("Content-Type", "")
            if not content_type.lower().startswith("multipart/form-data;"):
                self._send_error(415, "Expected multipart/form-data")
                return
            body = self.rfile.read(length)
            if len(body) != length:
                self._send_error(400, "Upload body ended before Content-Length")
                return
            try:
                files = _parse_multipart(content_type, body)
                filename, payload = files[0]
                if len(payload) > max_bytes:
                    limit = (
                        f"{max_bytes // (1024 * 1024)} MiB"
                        if max_bytes >= 1024 * 1024
                        else f"{max_bytes} bytes"
                    )
                    self._send_error(413, f"Upload exceeds the {limit} per-file limit")
                    return
                state = StateDB(library.state_db.path)
                request_library = LocalActivityLibrary(state, library.data_dir)
                try:
                    if Path(filename).suffix.lower() == ".zip":
                        results = request_library.import_archive_payload(
                            filename,
                            payload,
                            source_label=f"wifi:{filename}",
                        )
                    else:
                        results = [
                            request_library.import_payload(
                                filename,
                                payload,
                                source_label=f"wifi:{filename}",
                            )
                        ]
                finally:
                    state.close()
            except (ValueError, RuntimeError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            self._send_json(
                200,
                {
                    "imported": len(results),
                    "duplicates": sum(result.duplicate for result in results),
                    "activities": [
                        {
                            "id": result.fingerprint[:12],
                            "name": result.name,
                            "format": result.file_format,
                        }
                        for result in results
                    ],
                },
            )

        def log_message(self, format_string: str, *args: object) -> None:
            return

        def _send_error(self, status: int, message: str) -> None:
            self._send_json(status, {"error": message})

        def _send_json(self, status: int, payload: dict[str, object]) -> None:
            self._send(status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

        def _send(self, status: int, payload: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

    return TransferHandler


def _parse_multipart(content_type: str, body: bytes) -> list[tuple[str, bytes]]:
    headers = (
        f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("ascii", errors="strict")
        + body
    )
    message = BytesParser(policy=policy.default).parsebytes(headers)
    if not message.is_multipart():
        raise ValueError("Malformed multipart request")
    files: list[tuple[str, bytes]] = []
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data" or part.get_param("name", header="content-disposition") != "file":
            continue
        filename = part.get_filename()
        payload = part.get_payload(decode=True)
        if not filename or not isinstance(payload, bytes):
            raise ValueError("The multipart form must contain a file")
        if len(payload) > MAX_FILE_BYTES:
            raise ValueError(f"Upload exceeds the {MAX_FILE_BYTES // (1024 * 1024)} MiB per-file limit")
        files.append((Path(filename).name, payload))
    if len(files) != 1:
        raise ValueError("Upload exactly one file per request")
    return files
