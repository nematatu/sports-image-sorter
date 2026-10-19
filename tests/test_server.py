import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from PIL import Image

from sports_sorter.core import Database, Prediction, classify, scan
from sports_sorter.server import ReviewServer


class ExampleEngine:
    fingerprint = "synthetic-test-engine"

    def classify(self, pixels):
        return Prediction("テニス", 0.9, "Synthetic test fixture")


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name).resolve()
        source = root / "input"
        source.mkdir()
        Image.new("RGB", (80, 60), "green").save(source / "synthetic.jpg")
        self.db_path = root / "state.sqlite"
        with Database(self.db_path) as db:
            scan(source, db)
            classify(db, ExampleEngine())
        self.server = ReviewServer(("127.0.0.1", 0), self.db_path)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, data=None, headers=None):
        payload = json.dumps(data).encode() if data is not None else None
        request = Request(self.base + path, data=payload, headers=headers or {})
        with urlopen(request, timeout=5) as response:
            return response.read(), response.headers

    def test_grid_thumbnail_and_pagination(self):
        data = json.loads(self.request("/api/images?offset=0&limit=1")[0])
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["items"][0]["label"], "テニス")
        self.assertEqual(json.loads(self.request("/api/images?offset=1")[0])["items"], [])
        thumbnail, headers = self.request("/thumb/1")
        with Image.open(io.BytesIO(thumbnail)) as image:
            self.assertLessEqual(max(image.size), 320)
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertIn(b'lang="ja"', self.request("/")[0])

    def test_review_token_origin_and_atomic_labels(self):
        body = {"ids": [1], "label": "バドミントン", "human_verified": True, "evidence": "Independent human fixture"}
        with self.assertRaises(HTTPError) as absent:
            self.request("/api/review", body)
        self.assertEqual(absent.exception.code, 403)
        headers = {"X-Review-Token": self.server.token, "Origin": "https://example.invalid"}
        with self.assertRaises(HTTPError) as foreign:
            self.request("/api/review", body, headers)
        self.assertEqual(foreign.exception.code, 403)
        headers["Origin"] = self.base
        self.assertEqual(json.loads(self.request("/api/review", body, headers)[0])["updated"], 1)
        item = json.loads(self.request("/api/images")[0])["items"][0]
        self.assertEqual(item["review_label"], "バドミントン")
        self.assertEqual(item["human_verified"], 1)
        body.update(ids=[1, 999], label="水泳")
        with self.assertRaises(HTTPError):
            self.request("/api/review", body, headers)
        item = json.loads(self.request("/api/images")[0])["items"][0]
        self.assertEqual(item["label"], "バドミントン")

    def test_host_rebinding_and_nonloopback_rejected(self):
        with self.assertRaises(HTTPError) as foreign:
            self.request("/api/images", headers={"Host": "example.invalid"})
        self.assertEqual(foreign.exception.code, 403)
        with self.assertRaises(ValueError):
            ReviewServer(("0.0.0.0", 0), self.db_path)


if __name__ == "__main__":
    unittest.main()
