"""Compile only the repository's own fixed checker, never participant code.

This exercises compatibility with the bundled testlib calling convention even
on developer machines without isolate. Full judging remains integration-only.
"""
import io
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from judge.polygon import ExtractionBudget, extract
from tools.make_demo import problem_zip


@unittest.skipUnless(shutil.which("g++"), "g++ is not installed")
class CheckerTest(unittest.TestCase):
    def test_real_testlib_accepts_and_rejects(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            extract(io.BytesIO(problem_zip()), root, ExtractionBudget())
            built = subprocess.run(["g++", "-std=c++20", "-O2", str(root / "files/check.cpp"), "-o", str(root / "check")], capture_output=True, timeout=60)
            self.assertEqual(built.returncode, 0, built.stderr.decode())
            for content, expected in [("3\n", 0), ("4\n", 1), ("3 4\n", 1)]:
                (root / "output").write_text(content)
                checked = subprocess.run([str(root / "check"), str(root / "tests/01"), str(root / "output"), str(root / "tests/01.a")], capture_output=True, timeout=5)
                self.assertEqual(checked.returncode, expected, checked.stderr.decode())
