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
from judge.preparation import build_checker
from judge.sandbox import Result, compile_source
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

    def test_legacy_testlib_missing_stdint_is_fixed_only_for_checkers(self):
        # Our own minimal reproducer, not code from an uploaded package.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'files').mkdir()
            source = b'#include "testlib.h"\nint main(){return answer()==42 ? 0 : 1;}\n'
            header = b'inline uint64_t answer(){return 42;}\n'
            (root / 'files/check.cpp').write_bytes(source)
            (root / 'files/testlib.h').write_bytes(header)
            class TrustedCompiler:
                calls = []
                def run(self, argv, files, **kwargs):
                    self.calls.append(argv)
                    for name, content in files.items():
                        path = root / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_bytes(content)
                    result = subprocess.run(argv, cwd=root, capture_output=True, timeout=60)
                    return Result('OK' if result.returncode == 0 else 'RE', stderr=result.stderr,
                                  artifact=(root / 'main').read_bytes() if result.returncode == 0 else b'')
            compiler = TrustedCompiler()
            baseline = compile_source(compiler, 'cpp', source, {'files/testlib.h': header}, 'files/check.cpp')
            self.assertEqual(baseline.verdict, 'RE')
            self.assertIn(b'uint64_t', baseline.stderr)
            _, files = build_checker(compiler, root, {'checker': 'files/check.cpp', 'headers': ['files/testlib.h']})
            self.assertTrue(files['main'])
            self.assertNotIn('-include', compiler.calls[0])
            self.assertIn('-include', compiler.calls[1])
