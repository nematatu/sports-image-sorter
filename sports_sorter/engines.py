from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path
import platform
import shlex
import subprocess
import tempfile

from PIL import Image

from .core import LABELS, Prediction, protected_path

# These generic scene labels only nominate a sport. They do not establish an event context.
SPORT_IDENTIFIERS = {
    "badminton": "バドミントン", "tennis": "テニス", "volleyball": "バレーボール",
    "basketball": "バスケットボール", "track_and_field": "陸上競技", "athletics": "陸上競技",
    "swimming": "水泳", "table_tennis": "卓球", "soccer": "サッカー", "football_soccer": "サッカー",
    "gymnastics": "体操", "rhythmic_gymnastics": "新体操", "golf": "ゴルフ", "baseball": "野球",
    "softball": "ソフトボール", "rugby": "ラグビー", "handball": "ハンドボール",
    "field_hockey": "ホッケー", "ice_skating": "スケート", "skiing": "スキー",
    "cycling": "自転車競技", "wrestling": "格闘技", "judo": "格闘技",
}


def vision_prediction(observations: list[dict], threshold: float = 0.85, margin: float = 0.25) -> Prediction:
    candidates = {}
    signals = {}
    for observation in observations:
        identifier = observation["identifier"].lower().replace(" ", "_").replace("-", "_")
        signals[identifier] = float(observation["confidence"])
        label = SPORT_IDENTIFIERS.get(identifier)
        if label:
            score = float(observation["confidence"])
            candidates[label] = max(candidates.get(label, 0), score)
    ranked = sorted(candidates.items(), key=lambda item: item[1], reverse=True)
    raw = {"observations": observations, "candidates": candidates,
           "policy": "generic-scenes-conservative-v2", "threshold": threshold, "margin": margin}
    if not ranked:
        return Prediction(evidence="No sport-specific scene label; manual review required", raw=raw)
    label, score = ranked[0]
    competing = ranked[1][1] if len(ranked) > 1 else 0
    if score < threshold or score - competing < margin:
        return Prediction(score=score, evidence="Weak or competing sport candidates; manual review required", raw=raw)
    if any(signals.get(identifier, 0) >= 0.2 for identifier in ("ceremony", "podium", "medal")):
        return Prediction(score=score, evidence="Ceremony/podium/medal evidence; manual review required", raw=raw)
    context = {
        "バドミントン": ("racquet",), "テニス": ("racquet",),
        "バレーボール": ("ball",), "バスケットボール": ("ball",),
        "陸上競技": ("stadium",), "水泳": ("pool", "underwater"),
        "卓球": ("racquet",), "野球": ("baseball_bat",), "ソフトボール": ("baseball_bat",),
        "ゴルフ": ("golf_ball",), "サッカー": ("ball",), "自転車競技": ("bicycle",),
        "スキー": ("ski_equipment",),
    }
    corroboration = [identifier for identifier in context.get(label, ()) if signals.get(identifier, 0) >= 0.7]
    raw["context_signals"] = corroboration
    if signals.get("sport", 0) < 0.9 or not corroboration:
        return Prediction(score=score, evidence=f"Candidate {label}; insufficient scene/equipment corroboration; manual review required", raw=raw)
    return Prediction(label, score, f"Sport scene and {', '.join(corroboration)} support {label}; confidence is uncalibrated, review recommended", raw)


class VisionEngine:
    def __init__(self, revision: int = 2, threshold: float = 0.85, margin: float = 0.25, cache: str | Path | None = None):
        if platform.system() != "Darwin":
            raise RuntimeError("Apple Vision requires macOS")
        if not 0 <= threshold <= 1 or not 0 <= margin <= 1:
            raise ValueError("Threshold and margin must be in [0,1]")
        self.revision, self.threshold, self.margin = revision, threshold, margin
        source = Path(__file__).parent / "native" / "vision.swift"
        digest = hashlib.sha256(source.read_bytes()).hexdigest()[:16]
        self.cache = protected_path(cache or Path.home() / ".cache" / "sports-image-sorter")
        self.cache.mkdir(parents=True, exist_ok=True)
        self.binary = protected_path(self.cache / f"vision-{digest}")
        if not self.binary.exists():
            temporary = self.cache / f".build-{os.getpid()}"
            try:
                subprocess.run(["xcrun", "swiftc", str(source), "-o", str(temporary)], check=True,
                               capture_output=True, timeout=180)
                os.replace(temporary, self.binary)
            except subprocess.CalledProcessError as error:
                raise RuntimeError("Vision helper compilation failed") from error
            finally:
                temporary.unlink(missing_ok=True)
        capabilities = json.loads(subprocess.run([str(self.binary), "--capabilities"], check=True, capture_output=True, timeout=30).stdout)
        self.supported_revisions = capabilities["supportedRevisions"]
        if revision not in self.supported_revisions:
            raise ValueError(f"Unsupported revision; available revisions {self.supported_revisions}")
        config = {"engine": "apple-vision", "revision": revision, "policy": "generic-scenes-conservative-v2",
                  "threshold": threshold, "margin": margin, "helper": digest,
                  "macos": platform.mac_ver()[0], "preprocess": "rgb-orient-1024-v1"}
        self.fingerprint = "vision-" + hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:20]
        self.config = config

    def classify(self, pixels: Image.Image) -> Prediction:
        with tempfile.TemporaryDirectory(prefix="sports-sorter-") as directory:
            path = Path(directory) / "pixels.png"
            pixels.save(path, "PNG")
            result = subprocess.run([str(self.binary), str(self.revision), str(path)], check=True,
                                    capture_output=True, timeout=120)
        observations = json.loads(result.stdout)["observations"]
        return vision_prediction(observations, self.threshold, self.margin)


class CommandEngine:
    """Explicit local-process adapter. The operator controls the executable's network behavior."""
    def __init__(self, command: list[str], model_id: str):
        if not command or not model_id:
            raise ValueError("Command and stable model ID are required")
        self.command = command
        config = {"command": command, "model": model_id, "protocol": 1, "preprocess": "rgb-orient-1024-v1"}
        self.fingerprint = "command-" + hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()[:20]

    def classify(self, pixels: Image.Image) -> Prediction:
        with tempfile.TemporaryDirectory(prefix="sports-sorter-") as directory:
            path = Path(directory) / "pixels.png"
            pixels.save(path, "PNG")
            request = json.dumps({"schema_version": 1, "image_path": str(path), "labels": LABELS,
                                  "instructions": "Use visual equipment, venue and actions only. Do not infer identity. Uncertain, ambiguous, portraits, ceremonies and non-sport images must use 未分類."}, ensure_ascii=False)
            result = subprocess.run(self.command, input=request, text=True, capture_output=True, check=True, timeout=180)
        reply = json.loads(result.stdout)
        return Prediction(reply["label"], reply.get("score", 0), reply.get("evidence", ""), reply.get("raw"))


def load_engine(name: str, revision: int = 2, command: str | None = None, model_id: str | None = None):
    if name == "vision":
        return VisionEngine(revision=revision)
    if name == "clip":
        from .clip_engine import ClipEngine
        return ClipEngine()
    if name == "command":
        return CommandEngine(shlex.split(command or ""), model_id or "")
    if ":" in name:
        module, attribute = name.split(":", 1)
        return getattr(importlib.import_module(module), attribute)()
    raise ValueError("Choose vision, command, or module:factory")
