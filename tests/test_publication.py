from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


AUDITOR = Path(__file__).resolve().parents[1] / "scripts/audit_public.py"


class PublicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.git("init", "-b", "main")
        (self.root / "README.md").write_text("Synthetic publication fixture\n")
        self.git("add", "README.md")
        self.commit()

    def tearDown(self):
        self.temp.cleanup()

    def git(self, *args):
        return subprocess.check_output(["git", "-c", "user.name=Synthetic", "-c", "user.email=test@example.invalid", "-c", "core.hooksPath=/dev/null", *args], cwd=self.root, stderr=subprocess.DEVNULL)

    def commit(self):
        self.git("commit", "-m", "Synthetic fixture")

    def audit(self):
        return subprocess.run([sys.executable, str(AUDITOR)], cwd=self.root, capture_output=True, text=True)

    def test_source_only_repository_passes(self):
        self.assertEqual(self.audit().returncode, 0)

    def test_staged_private_data_is_rejected(self):
        (self.root / "runtime.json").write_text('{"synthetic": true}')
        self.git("add", "runtime.json")
        self.assertEqual(self.audit().returncode, 1)

    def test_deleted_private_path_remains_rejected_in_history(self):
        private = "/" + "Users/" + "synthetic/private-file"
        (self.root / "old.md").write_text(private)
        self.git("add", "old.md")
        self.commit()
        self.git("rm", "old.md")
        self.commit()
        result = self.audit()
        self.assertEqual(result.returncode, 1)
        self.assertIn("old.md", result.stderr)


if __name__ == "__main__":
    unittest.main()
