from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Protocol

from PIL import Image, ImageOps

LABELS = (
    "バドミントン", "テニス", "バレーボール", "バスケットボール", "陸上競技",
    "水泳", "卓球", "サッカー", "体操", "新体操", "ゴルフ", "野球", "ソフトボール",
    "ラグビー", "ハンドボール", "ホッケー", "スケート", "スキー", "自転車競技",
    "格闘技", "その他の競技", "未分類",
)
EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif"}


@dataclass(frozen=True)
class Prediction:
    label: str = "未分類"
    score: float = 0.0
    evidence: str = ""
    raw: object = None


class Engine(Protocol):
    fingerprint: str
    def classify(self, pixels: Image.Image) -> Prediction: ...


def protected_path(value: str | Path) -> Path:
    path = Path(os.path.abspath(Path(value).expanduser()))
    banned = Path.home() / "Downloads"
    if path == banned or banned in path.parents:
        raise ValueError("Access to Downloads is forbidden")
    for part in reversed((path, *path.parents)):
        if part.is_symlink():
            raise ValueError("Symbolic links are forbidden")
    return path


def _outside_source(path: Path, source: Path) -> None:
    if path == source or source in path.parents or path in source.parents:
        raise ValueError("State/output and source must not overlap")


def _read_file(path: Path):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError("Expected a regular file")
    return os.fdopen(fd, "rb")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with _read_file(protected_path(path)) as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_pixels(path: Path) -> Image.Image:
    with _read_file(protected_path(path)) as handle, Image.open(handle) as image:
        if getattr(image, "n_frames", 1) > 1:
            raise ValueError("Animated/multipage images require manual review")
        pixels = ImageOps.exif_transpose(image).convert("RGB")
        pixels.thumbnail((1024, 1024))
        pixels.info.clear()
        return pixels.copy()


def perceptual_hash(image: Image.Image) -> str:
    gray = image.convert("L").resize((9, 8))
    values = list(gray.tobytes())
    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | (values[y * 9 + x] > values[y * 9 + x + 1])
    return f"{bits:016x}"


class Database:
    def __init__(self, path: str | Path):
        self.path = protected_path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript("""
          CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS images (
            id INTEGER PRIMARY KEY, relpath TEXT UNIQUE NOT NULL,
            sha256 TEXT NOT NULL, size INTEGER NOT NULL, mtime_ns INTEGER NOT NULL,
            decode_status TEXT NOT NULL, decode_error TEXT, width INTEGER, height INTEGER,
            phash TEXT, present INTEGER NOT NULL DEFAULT 1);
          CREATE TABLE IF NOT EXISTS predictions (
            image_id INTEGER REFERENCES images(id), engine_key TEXT NOT NULL,
            label TEXT NOT NULL, score REAL NOT NULL, evidence TEXT NOT NULL,
            raw_json TEXT, error TEXT, created REAL NOT NULL,
            PRIMARY KEY(image_id,engine_key));
          CREATE TABLE IF NOT EXISTS reviews (
            image_id INTEGER PRIMARY KEY REFERENCES images(id), label TEXT NOT NULL,
            evidence TEXT NOT NULL, human_verified INTEGER NOT NULL, updated REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS copies (
            id INTEGER PRIMARY KEY, image_id INTEGER REFERENCES images(id),
            label TEXT NOT NULL, output_root TEXT NOT NULL, target_relpath TEXT NOT NULL,
            sha256 TEXT NOT NULL, status TEXT NOT NULL,
            UNIQUE(output_root,target_relpath));
        """)
        self.conn.commit()

    @property
    def source(self) -> Path:
        row = self.conn.execute("SELECT value FROM meta WHERE key='source'").fetchone()
        if row is None:
            raise ValueError("Scan an input directory first")
        return protected_path(row[0])

    def meta(self, key: str, default=None):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_meta(self, key: str, value: str):
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))
        self.conn.commit()

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def safe_source_file(db: Database, row: dict | sqlite3.Row) -> Path:
    relpath = Path(row["relpath"])
    if relpath.is_absolute() or ".." in relpath.parts:
        raise ValueError("Invalid source relative path")
    path = protected_path(db.source / relpath)
    if not path.is_file():
        raise FileNotFoundError("Source file unavailable")
    return path


def _error_name(error: Exception) -> str:
    # Paths and decoder metadata stay out of error fields and public diagnostics.
    return type(error).__name__


def scan(source: str | Path, db: Database) -> dict:
    root = protected_path(source)
    _outside_source(db.path, root)
    if not root.is_dir():
        raise NotADirectoryError("Input directory does not exist")
    old = db.meta("source")
    if old is not None and old != str(root):
        raise ValueError("A database belongs to exactly one input directory")
    files = []
    def onerror(error):
        raise error
    for directory, dirs, names in os.walk(root, followlinks=False, onerror=onerror):
        for name in dirs + names:
            entry = Path(directory) / name
            if entry.is_symlink():
                raise ValueError("Input contains a symbolic link")
        for name in names:
            entry = Path(directory) / name
            if entry.suffix.lower() in EXTENSIONS:
                if not stat.S_ISREG(entry.stat().st_mode):
                    raise ValueError("Input contains a non-regular file")
                files.append(entry)
    relative_files = {path.relative_to(root).as_posix() for path in files}
    known = {row[0] for row in db.conn.execute("SELECT relpath FROM images")}
    if known - relative_files:
        raise ValueError("Previously scanned source files are missing; use a new database")
    db.set_meta("source", str(root))
    scanned = []
    for path in sorted(files):
        before = path.stat()
        digest = sha256_file(path)
        status, error, width, height, phash = "ok", None, None, None, None
        try:
            pixels = load_pixels(path)
            width, height = pixels.size
            phash = perceptual_hash(pixels)
        except Exception as exc:
            status, error = "error", _error_name(exc)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns, before.st_ino) != (after.st_size, after.st_mtime_ns, after.st_ino):
            raise ValueError("Source changed during scan")
        relpath = path.relative_to(root).as_posix()
        existing = db.conn.execute("SELECT * FROM images WHERE relpath=?", (relpath,)).fetchone()
        if existing and existing["sha256"] != digest:
            raise ValueError("Previously scanned source content changed; use a new database")
        scanned.append((relpath, digest, after.st_size, after.st_mtime_ns, status, error, width, height, phash))
    with db.conn:
        db.conn.execute("UPDATE images SET present=0")
        for item in scanned:
            db.conn.execute("""INSERT INTO images
                (relpath,sha256,size,mtime_ns,decode_status,decode_error,width,height,phash,present)
                VALUES (?,?,?,?,?,?,?,?,?,1)
                ON CONFLICT(relpath) DO UPDATE SET present=1""", item)
    return {"images": len(scanned), "decodable": sum(item[4] == "ok" for item in scanned),
            "decode_errors": sum(item[4] == "error" for item in scanned),
            "formats": dict(Counter(Path(item[0]).suffix.lower() for item in scanned))}


def _validate_prediction(result: Prediction) -> Prediction:
    if result.label not in LABELS:
        raise ValueError("Engine returned an unsupported label")
    if not math.isfinite(result.score) or not 0 <= result.score <= 1:
        raise ValueError("Engine score must be finite and between 0 and 1")
    if not isinstance(result.evidence, str):
        raise ValueError("Engine evidence must be text")
    return result


def classify(db: Database, engine: Engine, retry: bool = False) -> dict:
    key = engine.fingerprint
    if not isinstance(key, str) or not key:
        raise ValueError("Engine fingerprint is required")
    counts = Counter()
    started = time.monotonic()
    for row in db.conn.execute("SELECT * FROM images WHERE present=1 ORDER BY id").fetchall():
        old = db.conn.execute("SELECT error FROM predictions WHERE image_id=? AND engine_key=?", (row["id"], key)).fetchone()
        if old is not None and (not retry or old["error"] is None):
            counts["skipped"] += 1
            continue
        error = None
        try:
            path = safe_source_file(db, row)
            if sha256_file(path) != row["sha256"]:
                raise ValueError("Source changed since scan")
            if row["decode_status"] != "ok":
                raise ValueError("Image could not be decoded")
            result = _validate_prediction(engine.classify(load_pixels(path)))
            if sha256_file(path) != row["sha256"]:
                raise ValueError("Source changed during inference")
        except Exception as exc:
            error = _error_name(exc)
            result = Prediction(evidence="Inference failed or source unavailable; manual review required")
        with db.conn:
            db.conn.execute("INSERT OR REPLACE INTO predictions VALUES (?,?,?,?,?,?,?,?)", (
                row["id"], key, result.label, result.score, result.evidence,
                json.dumps(result.raw, ensure_ascii=False), error, time.time()))
        counts["errors" if error else "processed"] += 1
    db.set_meta("selected_engine", key)
    return {**dict(counts), "engine_key": key, "elapsed_seconds": round(time.monotonic() - started, 3)}


def effective_rows(db: Database, engine_key: str | None = None) -> list[dict]:
    key = engine_key or db.meta("selected_engine", "")
    rows = db.conn.execute("""SELECT i.*, p.label prediction_label, p.score, p.evidence, p.error,
      p.engine_key, r.label review_label, r.evidence review_evidence,
      COALESCE(r.human_verified,0) human_verified,
      COALESCE(r.label,p.label,'未分類') label,
      (SELECT COUNT(DISTINCT p2.label)>1 FROM predictions p2
       WHERE p2.image_id=i.id AND p2.error IS NULL) disagreement
      FROM images i LEFT JOIN predictions p ON p.image_id=i.id AND p.engine_key=?
      LEFT JOIN reviews r ON r.image_id=i.id WHERE i.present=1 ORDER BY i.id""", (key,)).fetchall()
    return [dict(row) for row in rows]


def set_reviews(db: Database, image_ids: list[int], label: str, evidence: str = "", human_verified: bool = False) -> int:
    if label not in LABELS:
        raise ValueError("Unknown review label")
    if not image_ids or len(image_ids) > 10000 or not all(type(value) is int for value in image_ids):
        raise ValueError("Expected 1 to 10000 integer image IDs")
    if not isinstance(evidence, str) or len(evidence) > 10000 or type(human_verified) is not bool:
        raise ValueError("Invalid review evidence or verification flag")
    unique = set(image_ids)
    with db.conn:
        for image_id in unique:
            if db.conn.execute("SELECT 1 FROM images WHERE id=? AND present=1", (image_id,)).fetchone() is None:
                raise ValueError("Unknown image ID")
            db.conn.execute("INSERT OR REPLACE INTO reviews VALUES (?,?,?,?,?)", (image_id, label, evidence, int(human_verified), time.time()))
    return len(unique)


def _target(output: Path, relative: str) -> Path:
    part = Path(relative)
    if part.is_absolute() or ".." in part.parts:
        raise ValueError("Invalid output relative path")
    return protected_path(output / part)


def copy_images(db: Database, output: str | Path, engine_key: str | None = None, dry_run: bool = False) -> dict:
    root = protected_path(output)
    _outside_source(root, db.source)
    if db.path == root or db.path in root.parents or root in db.path.parents:
        raise ValueError("Output and state database must not overlap")
    counts = Counter()
    for row in effective_rows(db, engine_key):
        source = safe_source_file(db, row)
        if sha256_file(source) != row["sha256"]:
            raise ValueError("Source changed since scan; copying stopped")
        label = row["label"]
        previous = db.conn.execute("SELECT * FROM copies WHERE image_id=? AND label=? AND output_root=? ORDER BY id", (row["id"], label, str(root))).fetchall()
        verified = False
        for copy in previous:
            target = _target(root, copy["target_relpath"])
            if target.exists():
                if sha256_file(target) != row["sha256"]:
                    if copy["status"] == "complete":
                        raise ValueError("A recorded output copy is damaged; no overwrite performed")
                    continue
                if copy["status"] != "complete" and not dry_run:
                    with db.conn:
                        db.conn.execute("UPDATE copies SET status='complete' WHERE id=?", (copy["id"],))
                verified = True
                break
        if verified:
            counts["existing_verified"] += 1
            continue
        if dry_run:
            counts["would_copy"] += 1
            continue
        folder = protected_path(root / label)
        folder.mkdir(parents=True, exist_ok=True)
        suffix = source.suffix.lower()
        stem = source.stem
        serial = 0
        while True:
            name = f"{stem}{'' if serial == 0 else '__' + str(row['id']) + '_' + str(serial)}{suffix}"
            target = protected_path(folder / name)
            relative = target.relative_to(root).as_posix()
            if not target.exists() and db.conn.execute("SELECT 1 FROM copies WHERE output_root=? AND target_relpath=?", (str(root), relative)).fetchone() is None:
                break
            serial += 1
        with db.conn:
            cursor = db.conn.execute("INSERT INTO copies (image_id,label,output_root,target_relpath,sha256,status) VALUES (?,?,?,?,?,'pending')", (row["id"], label, str(root), relative, row["sha256"]))
            copy_id = cursor.lastrowid
        digest = hashlib.sha256()
        try:
            with _read_file(source) as incoming, target.open("xb") as outgoing:
                for block in iter(lambda: incoming.read(1024 * 1024), b""):
                    outgoing.write(block)
                    digest.update(block)
                outgoing.flush()
                os.fsync(outgoing.fileno())
            if digest.hexdigest() != row["sha256"] or sha256_file(target) != row["sha256"] or sha256_file(source) != row["sha256"]:
                raise ValueError("Copy/source integrity check failed")
        except BaseException:
            # Keep partial files as evidence. A retry selects a fresh exclusive path.
            with db.conn:
                db.conn.execute("UPDATE copies SET status='interrupted' WHERE id=?", (copy_id,))
            raise
        with db.conn:
            db.conn.execute("UPDATE copies SET status='complete' WHERE id=?", (copy_id,))
        counts["copied"] += 1
    return dict(counts)


def verify(db: Database, output: str | Path, engine_key: str | None = None) -> dict:
    root = protected_path(output)
    _outside_source(root, db.source)
    counts = Counter()
    problems = []
    rows = effective_rows(db, engine_key)
    for row in rows:
        try:
            source = safe_source_file(db, row)
            current = source.stat()
            intact = sha256_file(source) == row["sha256"] and current.st_size == row["size"] and current.st_mtime_ns == row["mtime_ns"]
        except (OSError, ValueError):
            intact = False
        counts["originals_unchanged" if intact else "originals_changed_or_missing"] += 1
        copies = db.conn.execute("SELECT * FROM copies WHERE image_id=? AND output_root=?", (row["id"], str(root))).fetchall()
        valid = False
        for copy in copies:
            if copy["label"] != row["label"]:
                counts["stale_copies"] += 1
                continue
            try:
                matches = sha256_file(_target(root, copy["target_relpath"])) == row["sha256"]
            except (OSError, ValueError):
                matches = False
            if matches:
                valid = True
                counts["verified_copies"] += 1
            else:
                counts["damaged_or_missing_copies"] += 1
        if not intact or not valid:
            problems.append(row["id"])
        counts["covered_images" if valid else "missing_images"] += 1
    return {"ok": not problems, "images": len(rows), **dict(counts), "problem_image_ids": problems}


def duplicates(db: Database, max_distance: int = 4, max_pairs: int = 1000) -> dict:
    rows = effective_rows(db)
    exact = defaultdict(list)
    for row in rows:
        exact[row["sha256"]].append(row["id"])
    near = []
    candidates = [row for row in rows if row["phash"] is not None]
    truncated = False
    for index, left in enumerate(candidates):
        for right in candidates[index + 1:]:
            if left["sha256"] == right["sha256"]:
                continue
            distance = (int(left["phash"], 16) ^ int(right["phash"], 16)).bit_count()
            if distance <= max_distance:
                if len(near) == max_pairs:
                    truncated = True
                    break
                near.append({"image_ids": [left["id"], right["id"]], "distance": distance})
        if truncated:
            break
    return {"exact": [ids for ids in exact.values() if len(ids) > 1], "similar_candidates": near,
            "similar_candidates_truncated": truncated, "note": "Perceptual similarity is a review hint, not proof of duplication"}


def summary(db: Database, engine_key: str | None = None) -> dict:
    rows = effective_rows(db, engine_key)
    counts = Counter(row["label"] for row in rows)
    gold = [row for row in rows if row["human_verified"] and row["prediction_label"] is not None]
    correct = sum(row["prediction_label"] == row["review_label"] for row in gold)
    return {"images": len(rows), "classified": sum(value for key, value in counts.items() if key != "未分類"),
            "unclassified": counts.get("未分類", 0), "by_label": dict(counts),
            "engine_key": engine_key or db.meta("selected_engine"),
            "decode_errors": sum(row["decode_status"] == "error" for row in rows),
            "inference_errors": sum(row["error"] is not None for row in rows),
            "disagreements": sum(bool(row["disagreement"]) for row in rows),
            "accuracy": {"human_verified_samples": len(gold), "correct": correct,
                         "coverage": len(gold) / len(rows) if rows else 0,
                         "value": correct / len(gold) if gold else None,
                         "note": "Accuracy is unavailable without independently human-verified labels" if not gold else "Accuracy on reviewed sample only; selection bias may apply"}}


def export_json(db: Database, path: str | Path) -> dict:
    target = protected_path(path)
    _outside_source(target, db.source)
    data = {"schema_version": 1, "summary": summary(db), "images": effective_rows(db), "duplicates": duplicates(db)}
    with target.open("x", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False, indent=2)
    return {"exported_images": len(data["images"])}


def get_predictions(db: Database, image_id: int) -> list[dict]:
    return [dict(row) for row in db.conn.execute("SELECT engine_key,label,score,evidence,raw_json,error FROM predictions WHERE image_id=? ORDER BY engine_key", (image_id,))]
