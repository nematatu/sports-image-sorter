import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from sports_sorter.core import (
    Database, Prediction, classify, copy_images, duplicates, effective_rows, export_json,
    load_pixels, protected_path, scan, set_reviews, sha256_file, summary, verify,
)
from sports_sorter.engines import CommandEngine, vision_prediction


class FixedEngine:
    def __init__(self, label="テニス", fingerprint="synthetic-test-v1"):
        self.label = label
        self.fingerprint = fingerprint
        self.calls = 0

    def classify(self, pixels):
        self.calls += 1
        assert not pixels.info
        assert max(pixels.size) <= 1024
        return Prediction(self.label, 0.9, "Synthetic test engine; not a real sport judgment")


class CoreTests(unittest.TestCase):
    def test_cli_reports_inference_failure_to_automation(self):
        from sports_sorter.cli import main
        self.image()
        scan(self.source, self.db)
        engine = FixedEngine()
        with patch("sports_sorter.cli.load_engine", return_value=engine), patch.object(engine, "classify", side_effect=TimeoutError):
            self.assertEqual(main(["--db", str(self.db.path), "classify"]), 1)

    def test_cli_rejects_database_inside_input_before_creation(self):
        from sports_sorter.cli import main
        path = self.source / "state.sqlite"
        self.assertEqual(main(["--db", str(path), "scan", str(self.source)]), 2)
        self.assertFalse(path.exists())

    def test_failed_inference_is_not_correct_unknown(self):
        self.image()
        scan(self.source, self.db)
        engine = FixedEngine()
        with patch.object(engine, "classify", side_effect=TimeoutError):
            classify(self.db, engine)
        set_reviews(self.db, [1], "未分類", human_verified=True)
        result = summary(self.db)["accuracy"]
        self.assertEqual(result["correct"], 0)
        self.assertIsNone(result["value"])
        self.assertEqual(result["reviewed_inference_failures"], 1)

    def test_rescan_recovers_temporary_decode_failure(self):
        self.image()
        with patch("sports_sorter.core.load_pixels", side_effect=OSError):
            self.assertEqual(scan(self.source, self.db)["decode_errors"], 1)
        self.assertEqual(scan(self.source, self.db)["decodable"], 1)
        engine = FixedEngine()
        self.assertEqual(classify(self.db, engine)["processed"], 1)
        self.assertEqual(effective_rows(self.db)[0]["decode_status"], "ok")

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "input"
        self.source.mkdir()
        self.output = self.root / "output"
        self.db = Database(self.root / "private" / "state.sqlite")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def image(self, name="one.png", color="blue"):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        Image.new("RGB", (64, 48), color).save(path)
        return path

    def prepare(self):
        self.image()
        scan(self.source, self.db)
        classify(self.db, FixedEngine())

    def test_copy_integrity_and_resume_without_overwrite(self):
        original = self.image()
        digest, mtime = sha256_file(original), original.stat().st_mtime_ns
        scan(self.source, self.db)
        classify(self.db, FixedEngine())
        self.output.joinpath("テニス").mkdir(parents=True)
        collision = self.output / "テニス" / "one.png"
        collision.write_bytes(b"preexisting")
        self.assertEqual(copy_images(self.db, self.output), {"copied": 1})
        self.assertEqual(collision.read_bytes(), b"preexisting")
        self.assertEqual(copy_images(self.db, self.output), {"existing_verified": 1})
        result = verify(self.db, self.output)
        self.assertTrue(result["ok"])
        self.assertEqual(result["originals_unchanged"], 1)
        self.assertEqual(original.stat().st_mtime_ns, mtime)
        self.assertEqual(sha256_file(original), digest)

    def test_dry_run_creates_no_output(self):
        self.prepare()
        self.assertEqual(copy_images(self.db, self.output, dry_run=True), {"would_copy": 1})
        self.assertFalse(self.output.exists())
        self.assertEqual(self.db.conn.execute("SELECT COUNT(*) FROM copies").fetchone()[0], 0)

    def test_classification_resume_and_fingerprint(self):
        self.image()
        scan(self.source, self.db)
        engine = FixedEngine()
        classify(self.db, engine)
        classify(self.db, engine)
        self.assertEqual(engine.calls, 1)
        alternate = FixedEngine("バレーボール", "synthetic-test-v2")
        classify(self.db, alternate)
        self.assertEqual(alternate.calls, 1)
        self.assertEqual(effective_rows(self.db)[0]["label"], "バレーボール")
        self.assertEqual(effective_rows(self.db)[0]["disagreement"], 1)

    def test_retry_failed_engine_only(self):
        self.image()
        scan(self.source, self.db)
        engine = FixedEngine()
        with patch.object(engine, "classify", side_effect=RuntimeError("private data")):
            self.assertEqual(classify(self.db, engine)["errors"], 1)
        self.assertEqual(effective_rows(self.db)[0]["error"], "RuntimeError")
        self.assertEqual(classify(self.db, engine)["skipped"], 1)
        self.assertEqual(classify(self.db, engine, retry=True)["processed"], 1)

    def test_decode_failure_is_unclassified_and_byte_preserved(self):
        path = self.source / "corrupt.png"
        path.write_bytes(b"not an image")
        self.assertEqual(scan(self.source, self.db)["decode_errors"], 1)
        classify(self.db, FixedEngine())
        self.assertEqual(effective_rows(self.db)[0]["label"], "未分類")
        copy_images(self.db, self.output)
        self.assertEqual((self.output / "未分類" / "corrupt.png").read_bytes(), b"not an image")
        self.assertTrue(verify(self.db, self.output)["ok"])

    def test_changed_source_stops_copy_and_rescan(self):
        self.prepare()
        self.image(color="red")
        with self.assertRaises(ValueError):
            copy_images(self.db, self.output)
        with self.assertRaises(ValueError):
            scan(self.source, self.db)
        self.assertFalse(verify(self.db, self.output)["ok"])

    def test_damaged_completed_copy_stops_without_overwrite(self):
        self.prepare()
        copy_images(self.db, self.output)
        target = self.output / "テニス" / "one.png"
        target.write_bytes(b"damaged")
        with self.assertRaises(ValueError):
            copy_images(self.db, self.output)
        self.assertEqual(target.read_bytes(), b"damaged")
        self.assertFalse(verify(self.db, self.output)["ok"])

    def test_interrupted_partial_copy_kept_and_retried_to_new_path(self):
        self.prepare()
        target = self.output / "テニス" / "one.png"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"partial")
        row = effective_rows(self.db)[0]
        with self.db.conn:
            self.db.conn.execute("INSERT INTO copies (image_id,label,output_root,target_relpath,sha256,status) VALUES (?,?,?,?,?,'interrupted')", (row["id"], "テニス", str(self.output), "テニス/one.png", row["sha256"]))
        self.assertEqual(copy_images(self.db, self.output), {"copied": 1})
        self.assertEqual(target.read_bytes(), b"partial")
        result = verify(self.db, self.output)
        self.assertTrue(result["ok"])
        self.assertEqual(result["damaged_or_missing_copies"], 1)

    def test_interrupted_complete_copy_recovered(self):
        self.prepare()
        copy_images(self.db, self.output)
        with self.db.conn:
            self.db.conn.execute("UPDATE copies SET status='pending'")
        self.assertEqual(copy_images(self.db, self.output), {"existing_verified": 1})
        self.assertEqual(self.db.conn.execute("SELECT status FROM copies").fetchone()[0], "complete")

    def test_review_provenance_accuracy_and_stale_copies(self):
        self.prepare()
        copy_images(self.db, self.output)
        image_id = effective_rows(self.db)[0]["id"]
        set_reviews(self.db, [image_id], "バレーボール", "Assistant visual review", False)
        self.assertIsNone(summary(self.db)["accuracy"]["value"])
        set_reviews(self.db, [image_id], "バレーボール", "Independent human review", True)
        accuracy = summary(self.db)["accuracy"]
        self.assertEqual(accuracy["human_verified_samples"], 1)
        self.assertEqual(accuracy["value"], 0)
        copy_images(self.db, self.output)
        result = verify(self.db, self.output)
        self.assertTrue(result["ok"])
        self.assertEqual(result["stale_copies"], 1)
        self.assertTrue((self.output / "テニス" / "one.png").exists())

    def test_reviews_transaction_and_validation(self):
        self.prepare()
        image_id = effective_rows(self.db)[0]["id"]
        with self.assertRaises(ValueError):
            set_reviews(self.db, [image_id], "../escape")
        with self.assertRaises(ValueError):
            set_reviews(self.db, [image_id, 9999999], "テニス")
        self.assertIsNone(effective_rows(self.db)[0]["review_label"])

    def test_duplicate_files_remain_separate_and_copy_every_path(self):
        original = self.image()
        shutil.copyfile(original, self.source / "two.png")
        scan(self.source, self.db)
        classify(self.db, FixedEngine())
        self.assertEqual(len(duplicates(self.db)["exact"][0]), 2)
        self.assertEqual(copy_images(self.db, self.output)["copied"], 2)
        self.assertEqual(verify(self.db, self.output)["covered_images"], 2)

    def test_export_does_not_overwrite(self):
        self.prepare()
        output = self.root / "result.json"
        export_json(self.db, output)
        self.assertEqual(json.loads(output.read_text())["schema_version"], 1)
        with self.assertRaises(FileExistsError):
            export_json(self.db, output)

    def test_missing_source_and_permission_error_are_not_empty_scan(self):
        with self.assertRaises(NotADirectoryError):
            scan(self.root / "missing", self.db)
        with patch("sports_sorter.core.os.walk") as walk:
            def failing(*args, **kwargs):
                kwargs["onerror"](PermissionError("denied"))
                return []
            walk.side_effect = failing
            with self.assertRaises(PermissionError):
                scan(self.source, self.db)

    def test_symlink_scan_and_symlink_ancestor_rejected(self):
        original = self.image()
        (self.source / "link.png").symlink_to(original)
        with self.assertRaises(ValueError):
            scan(self.source, self.db)
        alias = self.root / "alias"
        alias.symlink_to(self.source, target_is_directory=True)
        with self.assertRaises(ValueError):
            protected_path(alias / "one.png")

    def test_downloads_rejected_before_inspection(self):
        path = Path.home() / "Downloads" / "never-read.png"
        with patch.object(Path, "is_symlink", side_effect=AssertionError("Must not inspect Downloads")):
            with self.assertRaises(ValueError):
                protected_path(path)

    def test_output_and_state_source_overlap_rejected(self):
        self.prepare()
        with self.assertRaises(ValueError):
            copy_images(self.db, self.source / "output")
        with self.assertRaises(ValueError):
            copy_images(self.db, self.root)
        with self.assertRaises(ValueError):
            scan(self.root, self.db)

    def test_preprocessing_strips_metadata_and_limits_size(self):
        path = self.source / "large.jpg"
        image = Image.new("RGB", (2048, 1536), "white")
        exif = Image.Exif()
        exif[270] = "Private caption, not model input"
        image.save(path, exif=exif)
        pixels = load_pixels(path)
        self.assertEqual(pixels.size, (1024, 768))
        self.assertFalse(pixels.info)
        self.assertFalse(pixels.getexif())

    def test_portrait_or_ambiguous_generic_observations_stay_unclassified(self):
        result = vision_prediction([{"identifier": "tennis", "confidence": 0.99}])
        self.assertEqual(result.label, "未分類")
        self.assertEqual(result.raw["candidates"]["テニス"], 0.99)
        result = vision_prediction([{"identifier": "tennis", "confidence": 0.9}, {"identifier": "badminton", "confidence": 0.88}])
        self.assertEqual(result.label, "未分類")

    def test_native_context_and_ceremony_veto(self):
        observations = [{"identifier": "tennis", "confidence": 0.98},
                        {"identifier": "sport", "confidence": 0.99},
                        {"identifier": "racquet", "confidence": 0.95}]
        self.assertEqual(vision_prediction(observations).label, "テニス")
        observations.append({"identifier": "ceremony", "confidence": 0.3})
        self.assertEqual(vision_prediction(observations).label, "未分類")

    def test_missing_previously_scanned_source_is_not_forgotten(self):
        self.prepare()
        (self.source / "one.png").unlink()
        with self.assertRaises(ValueError):
            scan(self.source, self.db)
        self.assertFalse(verify(self.db, self.output)["ok"])

    def test_command_model_identifier_changes_fingerprint(self):
        first = CommandEngine(["example"], "model-a")
        second = CommandEngine(["example"], "model-b")
        self.assertNotEqual(first.fingerprint, second.fingerprint)


if __name__ == "__main__":
    unittest.main()
