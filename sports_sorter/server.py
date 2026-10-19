"""Loopback review UI with paginated images and explicit ground-truth entry."""

import argparse
import io
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import threading
from urllib.parse import parse_qs, urlsplit

from PIL import Image, ImageOps

from .core import LABELS, Database, effective_rows, get_predictions, safe_source_file, set_reviews, summary


class ReviewServer(ThreadingHTTPServer):
    def __init__(self, address, db_path, engine_key=None):
        if address[0] != "127.0.0.1":
            raise ValueError("レビューUIは127.0.0.1でのみ起動できます")
        self.db_path = Path(db_path)
        self.engine_key = engine_key
        self.token = secrets.token_urlsafe(32)
        self.thumb_cache = {}
        self.cache_lock = threading.Lock()
        self.decode_slots = threading.BoundedSemaphore(2)
        super().__init__(address, ReviewHandler)


class ReviewHandler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def _send(self, status, data, content_type="application/json; charset=utf-8"):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self'; script-src 'self'; style-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def _local_host(self):
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def do_GET(self):
        if not self._local_host():
            self._send(403, {"error": "許可されていないHostです"})
            return
        parsed = urlsplit(self.path)
        route = parsed.path
        static = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
        if route in static:
            types = {"/": "text/html; charset=utf-8", "/app.js": "text/javascript; charset=utf-8", "/style.css": "text/css; charset=utf-8"}
            self._send(200, (Path(__file__).parent / "web" / static[route]).read_bytes(), types[route])
            return
        try:
            with Database(self.server.db_path) as db:
                if route == "/api/images":
                    rows = effective_rows(db, self.server.engine_key)
                    query = parse_qs(parsed.query)
                    label = query.get("label", [""])[0]
                    mode = query.get("mode", ["all"])[0]
                    offset = max(0, int(query.get("offset", [0])[0]))
                    limit = min(100, max(1, int(query.get("limit", [48])[0])))
                    if label:
                        rows = [r for r in rows if r["label"] == label]
                    if mode == "unknown":
                        rows = [r for r in rows if r["label"] == "未分類"]
                    elif mode == "disagreement":
                        rows = [r for r in rows if r["disagreement"]]
                    elif mode == "unreviewed":
                        rows = [r for r in rows if r.get("review_label") is None]
                    elif mode == "error":
                        rows = [r for r in rows if r["decode_status"] == "error" or r.get("error")]
                    all_labels = list(LABELS)
                    self._send(200, {"items": rows[offset:offset+limit], "total": len(rows), "offset": offset, "labels": all_labels, "csrf": self.server.token, "summary": summary(db, self.server.engine_key)})
                    return
                if route.startswith("/api/predictions/"):
                    image_id = int(route.rsplit("/", 1)[1])
                    self._send(200, {"items": get_predictions(db, image_id)})
                    return
                if route.startswith("/thumb/") or route.startswith("/preview/"):
                    image_id = int(route.rsplit("/", 1)[1])
                    row = db.conn.execute("SELECT * FROM images WHERE id=? AND present=1", (image_id,)).fetchone()
                    if row is None:
                        self._send(404, {"error": "画像が見つかりません"})
                        return
                    source = safe_source_file(db, row)
                    stat = source.stat()
                    if stat.st_mtime_ns != row["mtime_ns"] or stat.st_size != row["size"]:
                        raise ValueError("画像が登録時から変更されています。verify で確認してください")
                    edge = 1600 if route.startswith("/preview/") else 320
                    key = (image_id, stat.st_mtime_ns, stat.st_size, edge)
                    with self.server.cache_lock:
                        data = self.server.thumb_cache.get(key)
                    if data is None:
                        with self.server.decode_slots, Image.open(source) as original:
                            image = ImageOps.exif_transpose(original)
                            image.thumbnail((edge, edge))
                            buffer = io.BytesIO()
                            image.convert("RGB").save(buffer, "JPEG", quality=82)
                            data = buffer.getvalue()
                        with self.server.cache_lock:
                            if len(self.server.thumb_cache) >= 128:
                                self.server.thumb_cache.pop(next(iter(self.server.thumb_cache)))
                            self.server.thumb_cache[key] = data
                    self._send(200, data, "image/jpeg")
                    return
                self._send(404, {"error": "見つかりません"})
        except (ValueError, OSError, KeyError) as error:
            self._send(400, {"error": str(error)})

    def do_POST(self):
        expected_origin = f"http://127.0.0.1:{self.server.server_port}"
        if not self._local_host() or self.headers.get("Origin") not in (None, expected_origin) or not secrets.compare_digest(self.headers.get("X-Review-Token", ""), self.server.token):
            self._send(403, {"error": "更新トークンまたはOriginが不正です"})
            return
        if self.path != "/api/review":
            self._send(404, {"error": "見つかりません"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            if not 0 < length <= 65536:
                raise ValueError("リクエストが大きすぎるか空です")
            data = json.loads(self.rfile.read(length))
            ids = data["ids"]
            if not isinstance(ids, list) or not 1 <= len(ids) <= 100 or any(type(i) is not int for i in ids):
                raise ValueError("1～100件の画像IDを指定してください")
            if not isinstance(data.get("label"), str) or not isinstance(data.get("evidence", ""), str) or type(data.get("human_verified", False)) is not bool:
                raise ValueError("ラベル・根拠・目視確認フラグの形式が不正です")
            with Database(self.server.db_path) as db:
                set_reviews(db, ids, data["label"], evidence=data.get("evidence", ""), human_verified=data.get("human_verified", False))
            self._send(200, {"updated": len(ids)})
        except (ValueError, KeyError, OSError) as error:
            self._send(400, {"error": str(error)})


def serve(db_path, port=8765, engine_key=None):
    with Database(db_path):
        pass
    server = ReviewServer(("127.0.0.1", port), db_path, engine_key)
    print(f"レビュー画面 http://127.0.0.1:{server.server_port}  終了 Ctrl+C", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="スポーツ画像のローカルレビュー")
    parser.add_argument("--db", required=True)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--engine-key")
    args = parser.parse_args()
    serve(args.db, args.port, args.engine_key)


if __name__ == "__main__":
    main()
