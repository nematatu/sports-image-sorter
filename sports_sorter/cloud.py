"""Opt-in Gemini classification with persistent, conservative spending reservations."""
from __future__ import annotations

import argparse
import base64
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
import hashlib
import http.client
import io
import json
import math
import os
from pathlib import Path
import sqlite3
import ssl
import sys
import time

from PIL import Image

from .core import LABELS, Database, Prediction, classify, protected_path

MODEL = "gemini-2.5-flash-lite"
INPUT_LIMIT = 32_000
OUTPUT_LIMIT = 512
NANODOLLARS = 1_000_000_000
RESERVATION = INPUT_LIMIT * 100 + OUTPUT_LIMIT * 400
POLICY = "visual-sport-gemini-v1"
PROMPT = """Classify the visible sporting activity in this image using equipment, court or venue,
uniforms, sporting actions and event signage. Never identify people or infer a sport from
appearance, athlete identity, nationality or fame. Return 未分類 for portraits, face closeups,
ordinary clothes, awards/podium/ceremonies, no visible sporting context, multiple plausible
sports, or uncertainty. A uniform alone is insufficient. Other clearly identifiable sports
outside the listed categories use その他の競技. is_sport_context must be false for any excluded
scene. Give only short visible evidence, no names or personal information. score is your
uncalibrated confidence from 0 to 1, not measured accuracy. Return one JSON object.
"""
SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "label": {"type": "STRING", "enum": list(LABELS)},
        "score": {"type": "NUMBER"},
        "evidence": {"type": "STRING"},
        "is_sport_context": {"type": "BOOLEAN"},
    },
    "required": ["label", "score", "evidence", "is_sport_context"],
}


class BudgetExceeded(RuntimeError):
    pass


class UncertainRequest(RuntimeError):
    pass


class CloudRequestError(RuntimeError):
    pass


def dollars_to_units(value: str | float | Decimal) -> int:
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0:
            raise ValueError("Budget must be positive and finite")
        return int(amount * NANODOLLARS)
    except InvalidOperation as error:
        raise ValueError("Invalid USD budget") from error


class BudgetLedger:
    def __init__(self, path: str | Path, max_usd: str | float):
        self.path = protected_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS budget (id INTEGER PRIMARY KEY CHECK(id=1), cap INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (
                request_key TEXT PRIMARY KEY, provider TEXT NOT NULL, model TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('reserved','success','uncertain')),
                reserved INTEGER NOT NULL, estimated INTEGER, input_tokens INTEGER,
                output_tokens INTEGER, prediction TEXT, error_kind TEXT, created REAL NOT NULL);
        """)
        cap = dollars_to_units(max_usd)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self.conn.execute("SELECT cap FROM budget WHERE id=1").fetchone()
            if row is None:
                self.conn.execute("INSERT INTO budget VALUES (1,?)", (cap,))
            elif cap > row[0]:
                raise ValueError("An existing budget cannot be increased; retain its approved cap")
            elif cap < row[0]:
                self.conn.execute("UPDATE budget SET cap=? WHERE id=1", (cap,))
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            self.conn.close()
            raise

    def reserve(self, key: str) -> Prediction | None:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            old = self.conn.execute("SELECT * FROM requests WHERE request_key=?", (key,)).fetchone()
            if old is not None:
                if old["status"] != "success":
                    raise UncertainRequest("Prior request may have been charged; automatic resending is disabled")
                result = Prediction(**json.loads(old["prediction"]))
                self.conn.commit()
                return result
            cap = self.conn.execute("SELECT cap FROM budget WHERE id=1").fetchone()[0]
            spent = self.conn.execute("SELECT COALESCE(SUM(reserved),0) FROM requests").fetchone()[0]
            if spent + RESERVATION > cap:
                raise BudgetExceeded("Conservative spending reservations would exceed the approved cap")
            self.conn.execute("""INSERT INTO requests
                (request_key,provider,model,status,reserved,created)
                VALUES (?,'gemini',?,'reserved',?,?)""", (key, MODEL, RESERVATION, time.time()))
            self.conn.commit()
            return None
        except BaseException:
            self.conn.rollback()
            raise

    def finish(self, key: str, prediction: Prediction, usage: dict):
        incoming = _token_count(usage.get("promptTokenCount"))
        outgoing = _token_count(usage.get("candidatesTokenCount"))
        thoughts = _token_count(usage.get("thoughtsTokenCount", 0))
        estimated = incoming * 100 + (outgoing + thoughts) * 400
        if incoming > INPUT_LIMIT or outgoing + thoughts > OUTPUT_LIMIT or estimated > RESERVATION:
            raise CloudRequestError("Reported usage exceeds the configured cost reservation")
        self.conn.execute("""UPDATE requests SET status='success',estimated=?,input_tokens=?,
            output_tokens=?,prediction=? WHERE request_key=? AND status='reserved'""",
            (estimated, incoming, outgoing + thoughts, json.dumps(asdict(prediction), ensure_ascii=False), key))

    def uncertain(self, key: str, error: BaseException):
        self.conn.execute("UPDATE requests SET status='uncertain',error_kind=? WHERE request_key=? AND status='reserved'",
                          (type(error).__name__, key))

    def summary(self) -> dict:
        row = self.conn.execute("""SELECT COUNT(*) requests, COALESCE(SUM(reserved),0) reserved,
            COALESCE(SUM(estimated),0) estimated, SUM(status='success') successful,
            SUM(status!='success') unresolved FROM requests""").fetchone()
        cap = self.conn.execute("SELECT cap FROM budget WHERE id=1").fetchone()[0]
        return {"cap_usd": cap / NANODOLLARS, "reserved_usd": row["reserved"] / NANODOLLARS,
                "estimated_successful_charge_usd": row["estimated"] / NANODOLLARS,
                "requests": row["requests"], "successful": row["successful"] or 0,
                "unresolved": row["unresolved"] or 0,
                "note": "Conservative reservations persist even on failure; usage estimates are not billing invoices"}

    def close(self):
        self.conn.close()


def _token_count(value) -> int:
    if type(value) is not int or value < 0:
        raise CloudRequestError("Missing or invalid usage token count")
    return value


def jpeg_pixels(pixels: Image.Image) -> bytes:
    rgb = pixels.convert("RGB")
    rgb.thumbnail((1024, 1024))
    clean = Image.frombytes("RGB", rgb.size, rgb.tobytes())
    buffer = io.BytesIO()
    clean.save(buffer, format="JPEG", quality=90, progressive=False)
    return buffer.getvalue()


def post_json(method: str, key: str, payload: dict) -> dict:
    connection = http.client.HTTPSConnection("generativelanguage.googleapis.com", timeout=120,
                                              context=ssl.create_default_context())
    try:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        connection.request("POST", f"/v1beta/models/{MODEL}:{method}", body=body,
                           headers={"Content-Type": "application/json", "x-goog-api-key": key})
        response = connection.getresponse()
        data = response.read(1_000_001)
        if response.status != 200:
            raise CloudRequestError(f"Gemini HTTP {response.status}; response body suppressed")
        if len(data) > 1_000_000:
            raise CloudRequestError("Gemini response too large")
        result = json.loads(data)
        if not isinstance(result, dict):
            raise CloudRequestError("Expected a JSON object")
        return result
    except (OSError, ValueError, http.client.HTTPException) as error:
        raise CloudRequestError("Gemini transport or JSON failure; no automatic retry") from error
    finally:
        connection.close()


def parse_prediction(response: dict, threshold: float = 0.85) -> Prediction:
    candidates = response.get("candidates", [])
    raw = {"model": MODEL, "policy": POLICY, "score_kind": "uncalibrated_model_self_report"}
    if response.get("promptFeedback", {}).get("blockReason") or not candidates:
        raw["refused"] = True
        return Prediction(evidence="Provider declined or returned no candidate; manual review required", raw=raw)
    if len(candidates) != 1 or candidates[0].get("finishReason") != "STOP":
        return Prediction(evidence="Provider response incomplete or blocked; manual review required", raw=raw)
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
    result = json.loads(text)
    if not isinstance(result, dict) or set(result) != {"label", "score", "evidence", "is_sport_context"}:
        raise CloudRequestError("Invalid classification fields")
    label, score, evidence, context = (result[k] for k in ("label", "score", "evidence", "is_sport_context"))
    if label not in LABELS or type(score) not in (int, float) or not math.isfinite(score) or not 0 <= score <= 1:
        raise CloudRequestError("Invalid classification label or score")
    if not isinstance(evidence, str) or len(evidence) > 2000 or type(context) is not bool:
        raise CloudRequestError("Invalid classification evidence or scene flag")
    raw.update({"proposed_label": label, "is_sport_context": context, "threshold": threshold})
    accepted = context and score >= threshold
    return Prediction(label if accepted else "未分類", float(score), evidence, raw)


class GeminiEngine:
    def __init__(self, api_key: str, ledger: BudgetLedger, *, allow_external: bool = False,
                 transport=post_json, threshold: float = 0.85):
        if not allow_external:
            raise ValueError("External image transmission requires explicit --allow-external approval")
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("GEMINI_API_KEY is not configured")
        if not math.isfinite(threshold) or not 0 <= threshold <= 1:
            raise ValueError("Threshold must be in [0,1]")
        self.api_key, self.ledger, self.transport, self.threshold = api_key.strip(), ledger, transport, threshold
        config = {"provider": "gemini", "model": MODEL, "policy": POLICY, "prompt": PROMPT,
                  "schema": SCHEMA, "threshold": threshold, "input_limit": INPUT_LIMIT,
                  "output_limit": OUTPUT_LIMIT, "preprocess": "fresh-rgb-jpeg90-longedge1024-v1"}
        self.fingerprint = "gemini-" + hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:24]

    def classify(self, pixels: Image.Image) -> Prediction:
        image = jpeg_pixels(pixels)
        request_key = hashlib.sha256(self.fingerprint.encode() + b"\0" + image).hexdigest()
        cached = self.ledger.reserve(request_key)
        if cached is not None:
            return cached
        payload = {
            "contents": [{"role": "user", "parts": [
                {"text": PROMPT}, {"inlineData": {"mimeType": "image/jpeg", "data": base64.b64encode(image).decode("ascii")}},
            ]}],
            "generationConfig": {"temperature": 0, "candidateCount": 1, "maxOutputTokens": OUTPUT_LIMIT,
                                 "thinkingConfig": {"thinkingBudget": 0},
                                 "responseMimeType": "application/json", "responseSchema": SCHEMA},
        }
        try:
            counted = self.transport("countTokens", self.api_key,
                                     {"generateContentRequest": {"model": f"models/{MODEL}", **payload}})
            if _token_count(counted.get("totalTokens")) > INPUT_LIMIT:
                raise CloudRequestError("Input token count exceeds the cost limit")
            response = self.transport("generateContent", self.api_key, payload)
            prediction = parse_prediction(response, self.threshold)
            self.ledger.finish(request_key, prediction, response.get("usageMetadata", {}))
            return prediction
        except BaseException as error:
            self.ledger.uncertain(request_key, error)
            raise


def read_env(path: str | Path) -> dict[str, str]:
    values = {}
    target = protected_path(path)
    with target.open(encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            name, separator, value = line.partition("=")
            name, value = name.strip(), value.strip()
            if separator and name in {"GEMINI_API_KEY", "TYPESAFE_API_KEY", "OPENAI_API_KEY"}:
                if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
                    value = value[1:-1]
                values[name] = value
    return values


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Opt-in Gemini classification with a persistent USD spending cap")
    parser.add_argument("--db", required=True)
    parser.add_argument("--budget-db", required=True)
    parser.add_argument("--env-file")
    parser.add_argument("--provider", choices=["gemini"], default="gemini")
    parser.add_argument("--max-usd", required=True)
    parser.add_argument("--allow-external", action="store_true")
    parser.add_argument("--retry", action="store_true", help="Retry local errors; uncertain API requests still cannot be resent")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    ledger = None
    try:
        if not args.allow_external:
            raise ValueError("Explicit --allow-external approval is required")
        with Database(args.db) as db:
            budget_path = protected_path(args.budget_db)
            if budget_path == db.source or db.source in budget_path.parents or budget_path in db.source.parents:
                raise ValueError("Budget ledger and source must not overlap")
            if budget_path == db.path:
                raise ValueError("Budget ledger must use a separate file")
            settings = read_env(args.env_file) if args.env_file else {}
            key = settings.get("GEMINI_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
            if args.dry_run:
                count = db.conn.execute("SELECT COUNT(*) FROM images WHERE present=1 AND decode_status='ok'").fetchone()[0]
                print(json.dumps({"provider": "gemini", "model": MODEL, "images": count,
                                  "maximum_new_reservations_usd": count * RESERVATION / NANODOLLARS,
                                  "api_calls": 0, "key_configured": bool(key)}, ensure_ascii=False))
                return 0
            ledger = BudgetLedger(budget_path, args.max_usd)
            engine = GeminiEngine(key, ledger, allow_external=True)
            result = classify(db, engine, retry=args.retry)
            print(json.dumps({"classification": result, "budget": ledger.summary()}, ensure_ascii=False))
            return 1 if result.get("errors") else 0
    except (OSError, ValueError, sqlite3.Error, RuntimeError) as error:
        print(json.dumps({"error": type(error).__name__, "message": str(error) if isinstance(error, (ValueError, BudgetExceeded, UncertainRequest, CloudRequestError)) else "Cloud setup failed; diagnostic suppressed"}), file=sys.stderr)
        return 1
    finally:
        if ledger is not None:
            ledger.close()


if __name__ == "__main__":
    raise SystemExit(main())
