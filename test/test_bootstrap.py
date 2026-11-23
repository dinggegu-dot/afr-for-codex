import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


BOOTSTRAP = Path(__file__).resolve().parents[1] / "plugins/afr-for-codex/runtime/bootstrap.py"


class BootstrapTests(unittest.TestCase):
    def test_does_not_provision_dependencies_in_a_foreign_state_root(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / "foreign-state"
            state.mkdir(mode=0o700)
            sentinel = state / "existing-note.txt"
            sentinel.write_text("synthetic existing contents", encoding="utf-8")
            original_mtime = sentinel.stat().st_mtime_ns

            result = subprocess.run(
                [sys.executable, "-I", "-B", str(BOOTSTRAP), "--doctor"],
                env={**os.environ, "AFR_FOR_CODEX_DATA_ROOT": str(state)},
                capture_output=True, text=True, timeout=5,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("unsafe_state_directory", result.stderr)
            self.assertEqual(result.stdout, "")
            self.assertEqual(list(state.iterdir()), [sentinel])
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "synthetic existing contents")
            self.assertEqual(sentinel.stat().st_mtime_ns, original_mtime)
