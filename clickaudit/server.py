"""点击审计批次系统 HTTP 服务（Python 标准库，零第三方依赖）。"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from .batches import BatchError, BatchStore

WEB_DIR = Path(__file__).parent / "web"

_STORE: BatchStore | None = None
_STORE_LOCK = threading.Lock()


def get_store() -> BatchStore:
    global _STORE
    if _STORE is None:
        with _STORE_LOCK:
            if _STORE is None:
                import os
                _STORE = BatchStore(os.environ.get("CLICKAUDIT_DATA", "/workspace/data"))
    return _STORE


class Handler(BaseHTTPRequestHandler):
    server_version = "ClickAudit/1.0"

    def log_message(self, fmt, *args):  # 精简日志
        pass

    def _send_json(self, obj, status: int = 200) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, rel: str) -> None:
        # 只允许 web 目录内文件（is_relative_to 阻止 ../ 穿越与同名前缀目录）
        web_root = WEB_DIR.resolve()
        target = (web_root / rel).resolve()
        if not target.is_relative_to(web_root) or not target.is_file():
            self.send_error(404)
            return
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
        }.get(target.suffix, "application/octet-stream")
        body = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0:
            raise BatchError("请求体为空")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as e:
            raise BatchError(f"JSON 解析失败：{e}") from None

    def do_GET(self):
        path = urlparse(self.path).path
        store = get_store()
        try:
            if path == "/api/batches":
                return self._send_json({"batches": store.list_batches()})
            if path.startswith("/api/batches/"):
                batch_id = path.rsplit("/", 1)[-1]
                return self._send_json(store.get_detail(batch_id))
            if path in ("/", "/index.html"):
                return self._send_static("index.html")
            if path.startswith("/static/"):
                return self._send_static(path[len("/static/"):])
            self.send_error(404)
        except BatchError as e:
            self._send_json({"error": str(e)}, 404)
        except Exception as e:  # 全局可恢复错误
            self._send_json({"error": f"服务器错误：{e}"}, 500)

    def do_POST(self):
        path = urlparse(self.path).path
        store = get_store()
        try:
            if path == "/api/batches":
                body = self._read_json()
                meta = store.create_batch(note=body.get("note", ""))
                return self._send_json(meta, 201)
            if path.endswith("/correction"):
                # /api/batches/<id>/correction
                parent = path.split("/")[3]
                body = self._read_json()
                meta = store.create_correction(parent, note=body.get("note", ""))
                return self._send_json(meta, 201)
            if "/submit/" in path:
                # /api/batches/<id>/submit/<kind>
                parts = [p for p in path.split("/") if p]
                batch_id, kind = parts[2], parts[4]
                payload = self._read_json()
                meta = store.submit(batch_id, kind, payload)
                return self._send_json(meta)
            if path.endswith("/rerun"):
                # /api/batches/<id>/rerun
                batch_id = path.split("/")[3]
                return self._send_json(store.rerun(batch_id))
            self.send_error(404)
        except BatchError as e:
            self._send_json({"error": str(e)}, 400)
        except Exception as e:
            self._send_json({"error": f"服务器错误：{e}"}, 500)


def serve(host: str = "127.0.0.1", port: int = 18080) -> None:
    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"ClickAudit 监听 http://{host}:{port}")
    httpd.serve_forever()


if __name__ == "__main__":
    import os
    serve(os.environ.get("HOST", "127.0.0.1"),
          int(os.environ.get("PORT", "18080")))
