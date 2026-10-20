import shutil
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from sports_sorter.core import Database, copy_images, scan, set_reviews, verify


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


if __name__ == "__main__":
    unittest.main()
