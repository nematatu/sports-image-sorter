import json
import multiprocessing
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from sports_sorter.core import Database, copy_images, export_json, scan, set_reviews, sha256_file, verify
from sports_sorter import reconcile


def copy_in_process(database, output, ready, results):
    try:
        ready.wait(10)
        with Database(database) as db:
            results.put(copy_images(db, output))
    except BaseException as error:
        results.put(type(error).__name__)


class OutputReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "input"
        self.source.mkdir()
        self.original = self.source / "one.png"
        Image.new("RGB", (64, 48), "blue").save(self.original)
        self.output = self.root / "output"
        self.db = Database(self.root / "state.sqlite")
        scan(self.source, self.db)
        set_reviews(self.db, [1], "テニス")

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def output_files(self):
        return [path for path in self.output.rglob("*") if path.is_file()]

    def test_fresh_database_adopts_existing_output(self):
        copy_images(self.db, self.output)
        with Database(self.root / "fresh.sqlite") as fresh:
            scan(self.source, fresh)
            set_reviews(fresh, [1], "テニス")
            copy_images(fresh, self.output)
            self.assertTrue(verify(fresh, self.output)["ok"])
        self.assertEqual(len(self.output_files()), 1)

    def test_relabel_archives_previous_destination(self):
        copy_images(self.db, self.output)
        set_reviews(self.db, [1], "未分類")
        result = copy_images(self.db, self.output)
        self.assertEqual(len(self.output_files()), 1)
        self.assertEqual(self.output_files()[0].parent.name, "未分類")
        archive = Path(result["archive"])
        self.assertEqual((archive / "テニス" / "one.png").read_bytes(), self.original.read_bytes())

    def test_exact_source_aliases_have_one_output(self):
        shutil.copyfile(self.original, self.source / "alias.png")
        scan(self.source, self.db)
        set_reviews(self.db, [2], "テニス")
        result = copy_images(self.db, self.output)
        self.assertEqual(len(self.output_files()), 1)
        self.assertEqual(result["source_images"], 2)
        self.assertEqual(result["unique_images"], 1)
        self.assertEqual(verify(self.db, self.output)["covered_images"], 2)

    def test_verify_rejects_untracked_cross_label_duplicate(self):
        copy_images(self.db, self.output)
        extra = self.output / "未分類" / "untracked.png"
        extra.parent.mkdir()
        shutil.copyfile(self.original, extra)
        self.assertFalse(verify(self.db, self.output)["ok"])


    def test_same_basename_different_content_is_not_overwritten(self):
        other = self.source / "subfolder" / "one.png"
        other.parent.mkdir()
        Image.new("RGB", (64, 48), "red").save(other)
        scan(self.source, self.db)
        set_reviews(self.db, [2], "テニス")
        result = copy_images(self.db, self.output)
        self.assertEqual(result["copied"], 2)
        self.assertEqual({sha256_file(path) for path in self.output_files()},
                         {sha256_file(self.original), sha256_file(other)})
        self.assertEqual(len({path.name for path in self.output_files()}), 2)
        self.assertTrue(verify(self.db, self.output)["ok"])

    def test_alias_label_conflict_is_unclassified_and_exported(self):
        shutil.copyfile(self.original, self.source / "two.png")
        scan(self.source, self.db)
        set_reviews(self.db, [2], "バレーボール")
        result = copy_images(self.db, self.output)
        self.assertEqual(result["conflicting_groups"], 1)
        self.assertEqual(result["contents"][0]["image_ids"], [1, 2])
        self.assertEqual(set(result["contents"][0]["conflicting_labels"]), {"テニス", "バレーボール"})
        self.assertEqual(self.output_files()[0].parent.name, "未分類")
        export = self.root / "result.json"
        export_json(self.db, export)
        self.assertEqual(json.loads(export.read_text())["output_plan"]["contents"], result["contents"])
        self.assertTrue(verify(self.db, self.output)["ok"])

    def test_dry_run_leaves_database_and_tree_unchanged(self):
        copy_images(self.db, self.output)
        extra = self.output / "unknown.bin"
        extra.write_bytes(b"preserve unrelated existing content")
        set_reviews(self.db, [1], "未分類")
        before_rows = [tuple(row) for row in self.db.conn.execute("SELECT * FROM copies")]
        before_files = {str(path.relative_to(self.root)): path.read_bytes()
                        for path in self.root.rglob("*") if path.is_file()}
        result = copy_images(self.db, self.output, dry_run=True)
        self.assertEqual(result["would_copy"], 1)
        self.assertEqual(result["would_archive"], 2)
        self.assertEqual(before_rows, [tuple(row) for row in self.db.conn.execute("SELECT * FROM copies")])
        self.assertEqual(before_files, {str(path.relative_to(self.root)): path.read_bytes()
                                      for path in self.root.rglob("*") if path.is_file()})

    def test_dry_run_creates_no_lock_or_parent_directory(self):
        output = self.root / "new-parent" / "output"
        copy_images(self.db, output, dry_run=True)
        self.assertFalse(output.parent.exists())

    def test_only_exact_regular_ds_store_is_ignored(self):
        copy_images(self.db, self.output)
        metadata = self.output / ".DS_Store"
        metadata.write_bytes(b"opaque OS metadata")
        self.assertTrue(verify(self.db, self.output)["ok"])
        result = copy_images(self.db, self.output)
        self.assertEqual(result["archived"], 0)
        self.assertEqual(metadata.read_bytes(), b"opaque OS metadata")
        (self.output / ".DS_Store.backup").write_bytes(b"other file")
        self.assertFalse(verify(self.db, self.output)["ok"])

    def test_symlinks_and_special_files_stop_before_mutation(self):
        self.output.mkdir()
        alias = self.output / "link"
        alias.symlink_to(self.source, target_is_directory=True)
        self.assertFalse(verify(self.db, self.output)["ok"])
        with self.assertRaises(ValueError):
            copy_images(self.db, self.output)
        self.assertFalse((self.root / ".output.sports-sorter.lock").exists())
        alias.unlink()
        fifo = self.output / "pipe"
        os.mkfifo(fifo)
        self.assertFalse(verify(self.db, self.output)["ok"])
        with self.assertRaises(ValueError):
            copy_images(self.db, self.output)
        self.assertTrue(fifo.exists())

    def test_all_source_metadata_is_checked_before_output_mutation(self):
        second = self.source / "two.png"
        Image.new("RGB", (64, 48), "red").save(second)
        scan(self.source, self.db)
        current = second.stat()
        os.utime(second, ns=(current.st_atime_ns, current.st_mtime_ns + 1000000000))
        with self.assertRaises(ValueError):
            copy_images(self.db, self.output)
        self.assertFalse(self.output.exists())
        self.assertFalse((self.root / ".output.sports-sorter.lock").exists())
        self.assertEqual(list(self.root.glob("output-archive-*")), [])

    def test_interrupted_complete_temporary_is_never_canonical(self):
        class InterruptedReader:
            def __init__(self, incoming):
                self.incoming = incoming
                self.reads = 0
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.incoming.close()
            def read(self, size):
                self.reads += 1
                if self.reads > 1:
                    raise KeyboardInterrupt()
                return self.incoming.read(size)
        with patch("sports_sorter.reconcile._read_file",
                   side_effect=lambda path: InterruptedReader(path.open("rb"))):
            with self.assertRaises(KeyboardInterrupt):
                copy_images(self.db, self.output)
        partial, = self.output_files()
        self.assertEqual(partial.suffix, ".partial")
        self.assertEqual(partial.read_bytes(), self.original.read_bytes())
        self.assertFalse(verify(self.db, self.output)["ok"])
        result = copy_images(self.db, self.output)
        self.assertEqual(result["copied"], 1)
        self.assertEqual(result["archived"], 1)
        self.assertEqual(self.output_files()[0].suffix, ".png")
        self.assertEqual((Path(result["archive"]) / partial.relative_to(self.output)).read_bytes(), self.original.read_bytes())
        self.assertTrue(verify(self.db, self.output)["ok"])

    def test_copy_failure_keeps_last_good_old_destination(self):
        copy_images(self.db, self.output)
        set_reviews(self.db, [1], "未分類")
        old = self.output / "テニス" / "one.png"
        with patch("sports_sorter.reconcile.os.link", side_effect=OSError("synthetic interruption")):
            with self.assertRaises(OSError):
                copy_images(self.db, self.output)
        self.assertEqual(old.read_bytes(), self.original.read_bytes())
        self.assertFalse(verify(self.db, self.output)["ok"])
        copy_images(self.db, self.output)
        self.assertTrue(verify(self.db, self.output)["ok"])
        self.assertEqual(len(self.output_files()), 1)

    def test_interrupted_archive_rerun_converges_without_loss(self):
        copy_images(self.db, self.output)
        for index in range(2):
            extra = self.output / "未分類" / f"duplicate-{index}.png"
            extra.parent.mkdir(exist_ok=True)
            shutil.copyfile(self.original, extra)
        rename = reconcile.os.rename
        calls = 0
        def interrupt(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise KeyboardInterrupt()
            return rename(source, destination)
        with patch("sports_sorter.reconcile.os.rename", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                copy_images(self.db, self.output)
        preserved = self.output_files() + [path for archive in self.root.glob("output-archive-*")
                                           for path in archive.rglob("*") if path.is_file()]
        self.assertEqual(len(preserved), 3)
        self.assertTrue(all(path.read_bytes() == self.original.read_bytes() for path in preserved))
        copy_images(self.db, self.output)
        self.assertTrue(verify(self.db, self.output)["ok"])
        self.assertEqual(copy_images(self.db, self.output)["archived"], 0)
        self.assertEqual(len(self.output_files()), 1)

    def test_review_change_stops_archival_then_retry_converges(self):
        copy_images(self.db, self.output)
        set_reviews(self.db, [1], "バレーボール")
        copy_content = reconcile._copy_content
        def change_review(*args):
            result = copy_content(*args)
            set_reviews(self.db, [1], "未分類")
            return result
        with patch("sports_sorter.reconcile._copy_content", side_effect=change_review):
            with self.assertRaises(ValueError):
                copy_images(self.db, self.output)
        self.assertTrue((self.output / "テニス" / "one.png").exists())
        self.assertEqual(list(self.root.glob("output-archive-*")), [])
        copy_images(self.db, self.output)
        self.assertTrue(verify(self.db, self.output)["ok"])
        self.assertEqual(self.output_files()[0].parent.name, "未分類")

    def test_concurrent_different_databases_share_output_lock(self):
        fresh_path = self.root / "fresh.sqlite"
        with Database(fresh_path) as fresh:
            scan(self.source, fresh)
            set_reviews(fresh, [1], "テニス")
        context = multiprocessing.get_context("spawn")
        ready, results = context.Event(), context.Queue()
        workers = [context.Process(target=copy_in_process, args=(str(path), str(self.output), ready, results))
                   for path in (self.db.path, fresh_path)]
        try:
            for worker in workers:
                worker.start()
            ready.set()
            responses = [results.get(timeout=20) for _ in workers]
            for worker in workers:
                worker.join(20)
                self.assertEqual(worker.exitcode, 0)
            self.assertTrue(all(isinstance(response, dict) for response in responses), responses)
            self.assertEqual(sum(response["copied"] for response in responses), 1)
            self.assertEqual(len(self.output_files()), 1)
            self.assertTrue(verify(self.db, self.output)["ok"])
        finally:
            for worker in workers:
                if worker.is_alive():
                    worker.terminate()
                    worker.join()
            results.close()


if __name__ == "__main__":
    unittest.main()
