import base64
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import tempfile
import unittest

from PIL import Image

from sports_sorter.cloud import (
    BudgetExceeded, BudgetLedger, CloudRequestError, GeminiEngine, INPUT_LIMIT,
    MODEL, NANODOLLARS, RESERVATION, UncertainRequest, jpeg_pixels, parse_prediction, read_env,
)


def response(label="テニス", score=0.95, context=True, scene="sport_action"):
    result = {"label": label, "score": score, "is_sport_context": context,
              "evidence": "Visible racket, ball and tennis court during play", "scene_type": scene}
    return {"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": json.dumps(result)}]}}],
            "usageMetadata": {"promptTokenCount": 500, "candidatesTokenCount": 80}}


class CloudTests(unittest.TestCase):
    def test_excluded_scene_overrides_confident_sport(self):
        result = parse_prediction(response(score=0.99, context=True, scene="studio_portrait"))
        self.assertEqual(result.label, "未分類")
        self.assertEqual(result.raw["proposed_label"], "テニス")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name).resolve() / "budget.sqlite"
        self.ledger = BudgetLedger(self.path, "1")
        self.calls = []
        self.pixels = Image.new("RGB", (1600, 800), "blue")

    def tearDown(self):
        self.ledger.close()
        self.temp.cleanup()

    def transport(self, method, key, payload):
        self.calls.append((method, key, payload))
        return {"totalTokens": 500} if method == "countTokens" else response()

    def engine(self, **kwargs):
        return GeminiEngine("synthetic-key", self.ledger, allow_external=True,
                            transport=kwargs.pop("transport", self.transport), **kwargs)

    def test_requires_permission_and_key(self):
        with self.assertRaises(ValueError):
            GeminiEngine("synthetic", self.ledger)
        with self.assertRaises(ValueError):
            GeminiEngine("", self.ledger, allow_external=True)
        self.assertEqual(self.calls, [])

    def test_image_reencoding_strips_metadata_and_limits_dimensions(self):
        self.pixels.info["comment"] = b"private-name"
        self.pixels.info["exif"] = b"private-metadata"
        encoded = jpeg_pixels(self.pixels)
        self.assertNotIn(b"private", encoded)
        with Image.open(io.BytesIO(encoded)) as clean:
            self.assertEqual(clean.mode, "RGB")
            self.assertEqual(clean.size, (1024, 512))
            self.assertFalse(clean.getexif())
            self.assertNotIn("comment", clean.info)

    def test_classifies_pixels_only_and_records_usage(self):
        predicted = self.engine().classify(self.pixels)
        self.assertEqual(predicted.label, "テニス")
        self.assertEqual([call[0] for call in self.calls], ["countTokens", "generateContent"])
        counted = self.calls[0][2]["generateContentRequest"]
        generated = self.calls[1][2]
        self.assertEqual(counted["model"], "models/" + MODEL)
        self.assertEqual(counted["contents"], generated["contents"])
        self.assertEqual(generated["generationConfig"]["maxOutputTokens"], 512)
        encoded = generated["contents"][0]["parts"][1]["inlineData"]["data"]
        self.assertEqual(base64.b64decode(encoded), jpeg_pixels(self.pixels))
        payload_text = json.dumps(generated)
        self.assertNotIn("synthetic-key", payload_text)
        self.assertNotIn("image_path", payload_text)
        summary = self.ledger.summary()
        self.assertEqual(summary["reserved_usd"], RESERVATION / NANODOLLARS)
        self.assertEqual(summary["estimated_successful_charge_usd"], (500 * 300 + 80 * 2500) / NANODOLLARS)
        stored = self.ledger.conn.execute("SELECT prediction FROM requests").fetchone()[0]
        self.assertNotIn(encoded, stored)
        self.assertNotIn("synthetic-key", stored)

    def test_repeated_success_uses_persistent_cache(self):
        first = self.engine().classify(self.pixels)
        self.ledger.close()
        self.ledger = BudgetLedger(self.path, "1")
        second = self.engine().classify(self.pixels)
        self.assertEqual(first, second)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.ledger.summary()["requests"], 1)

    def test_timeout_reservation_persists_and_blocks_resending(self):
        def failing(method, key, payload):
            self.calls.append(method)
            raise TimeoutError("synthetic timeout")
        engine = self.engine(transport=failing)
        with self.assertRaises(TimeoutError):
            engine.classify(self.pixels)
        with self.assertRaises(UncertainRequest):
            self.engine().classify(self.pixels)
        self.assertEqual(self.calls, ["countTokens"])
        self.assertEqual(self.ledger.summary()["unresolved"], 1)
        self.assertEqual(self.ledger.summary()["reserved_usd"], RESERVATION / NANODOLLARS)

    def test_crashed_reserved_request_cannot_be_retried(self):
        self.ledger.reserve("synthetic-request")
        self.ledger.close()
        self.ledger = BudgetLedger(self.path, "1")
        with self.assertRaises(UncertainRequest):
            self.ledger.reserve("synthetic-request")

    def test_budget_blocks_before_network(self):
        self.ledger.close()
        self.ledger = BudgetLedger(self.path, "0.001")
        with self.assertRaises(BudgetExceeded):
            self.engine().classify(self.pixels)
        self.assertEqual(self.calls, [])

    def test_existing_budget_cannot_increase(self):
        with self.assertRaises(ValueError):
            BudgetLedger(self.path, "2")

    def test_concurrent_reservations_obey_shared_cap(self):
        self.ledger.close()
        self.ledger = BudgetLedger(self.path, str(RESERVATION / NANODOLLARS))
        def reserve(index):
            ledger = BudgetLedger(self.path, str(RESERVATION / NANODOLLARS))
            try:
                ledger.reserve(str(index))
                return "reserved"
            except BudgetExceeded:
                return "blocked"
            finally:
                ledger.close()
        with ThreadPoolExecutor(max_workers=4) as workers:
            outcomes = list(workers.map(reserve, range(4)))
        self.assertEqual(outcomes.count("reserved"), 1)
        self.assertEqual(outcomes.count("blocked"), 3)

    def test_input_token_limit_prevents_generation(self):
        def large(method, key, payload):
            self.calls.append(method)
            return {"totalTokens": INPUT_LIMIT + 1}
        with self.assertRaises(CloudRequestError):
            self.engine(transport=large).classify(self.pixels)
        self.assertEqual(self.calls, ["countTokens"])

    def test_unknown_labels_and_nonfinite_scores_rejected(self):
        for label, score in [("unlisted", 0.9), ("テニス", float("nan")), ("テニス", True), ("テニス", 1.1)]:
            with self.subTest(label=label, score=score), self.assertRaises(CloudRequestError):
                parse_prediction(response(label, score))

    def test_weak_or_missing_context_goes_to_review(self):
        self.assertEqual(parse_prediction(response(score=0.4)).label, "未分類")
        self.assertEqual(parse_prediction(response(context=False)).label, "未分類")

    def test_refusal_and_truncation_go_to_review(self):
        self.assertEqual(parse_prediction({"promptFeedback": {"blockReason": "SAFETY"}}).label, "未分類")
        truncated = response()
        truncated["candidates"][0]["finishReason"] = "MAX_TOKENS"
        self.assertEqual(parse_prediction(truncated).label, "未分類")

    def test_invalid_usage_keeps_reservation(self):
        def no_usage(method, key, payload):
            if method == "countTokens":
                return {"totalTokens": 500}
            result = response()
            del result["usageMetadata"]
            return result
        with self.assertRaises(CloudRequestError):
            self.engine(transport=no_usage).classify(self.pixels)
        self.assertEqual(self.ledger.summary()["unresolved"], 1)

    def test_env_parser_loads_only_supported_keys_without_execution(self):
        path = Path(self.temp.name).resolve() / "test.env"
        path.write_text('# synthetic\nGEMINI_API_KEY="synthetic"\nOTHER=ignored\nTYPESAFE_API_KEY=\n', encoding="utf-8")
        self.assertEqual(read_env(path), {"GEMINI_API_KEY": "synthetic", "TYPESAFE_API_KEY": ""})


if __name__ == "__main__":
    unittest.main()
