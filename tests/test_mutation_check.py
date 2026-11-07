"""mutation_check.py: mutators, diff parsing, scoring, and above all: the repo is always restored."""
import ast
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _paths

mc = _paths.load(_paths.MUTATION_CHECK, "mutation_check")

SRC = """def clamp(x, lo, hi):
    if x < lo:
        return lo
    if x > hi:
        return hi
    return x


def total(a, b):
    return a + b * 2
"""
WEAK = """import unittest
import m


class T(unittest.TestCase):
    def test_ok(self):
        self.assertIsNotNone(m.clamp(5, 0, 10))
        self.assertTrue(m.total(1, 2))
"""
STRONG = """import unittest
import m


class T(unittest.TestCase):
    def test_clamp(self):
        self.assertEqual(m.clamp(-1, 0, 10), 0)
        self.assertEqual(m.clamp(0, 0, 10), 0)
        self.assertEqual(m.clamp(5, 0, 10), 5)
        self.assertEqual(m.clamp(10, 0, 10), 10)
        self.assertEqual(m.clamp(11, 0, 10), 10)

    def test_total(self):
        self.assertEqual(m.total(1, 2), 5)
        self.assertEqual(m.total(0, 0), 0)
"""
CMD = f'"{sys.executable}" -m unittest discover -s tests'


def git(cwd, *a):
    subprocess.run(["git", *a], cwd=cwd, check=True, capture_output=True, text=True)


class Repo:
    """A throwaway git repo: HEAD has a stub m.py, the working tree has the real one plus tests."""

    def __init__(self, tests):
        self.dir = Path(tempfile.mkdtemp(prefix="ht-test-"))
        git(self.dir, "init", "-q")
        git(self.dir, "config", "user.email", "t@example.com")
        git(self.dir, "config", "user.name", "t")
        (self.dir / "m.py").write_text("def clamp(x, lo, hi):\n    return x\n", encoding="utf-8")
        git(self.dir, "add", "-A")
        git(self.dir, "commit", "-qm", "base")
        (self.dir / "m.py").write_text(SRC, encoding="utf-8")
        (self.dir / "tests").mkdir()
        (self.dir / "tests" / "test_m.py").write_text(tests, encoding="utf-8")

    def run(self, *extra):
        return subprocess.run([sys.executable, str(_paths.MUTATION_CHECK), "--cmd", CMD, *extra],
                              cwd=self.dir, capture_output=True, text=True)

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


class MutatorTests(unittest.TestCase):
    def test_python_sites_only_on_changed_lines(self):
        sites = mc.py_sites(SRC, {2})
        self.assertTrue(sites)
        self.assertEqual({s["line"] for s in sites}, {2})
        labels = {s["label"] for s in sites}
        self.assertIn("`<` -> `>=`", labels)
        self.assertIn("`<` -> `<=`", labels)

    def test_python_mutation_kinds(self):
        src = ("def f(a, b, flag):\n    if a and b == 3:\n        log(a)\n        return a + 1\n"
               "    return not flag or True\n")
        labels = {s["label"] for s in mc.py_sites(src, None)}
        for want in ("`and` -> `or`", "`==` -> `!=`", "`3` -> `4`", "`3` -> `2`", "`+` -> `-`",
                     "`return <value>` -> `return None`", "remove `not`", "`True` -> `False`",
                     "remove call statement", "negate `if` condition"):
            self.assertIn(want, labels)

    def test_python_apply_produces_valid_code_with_the_change(self):
        s = [x for x in mc.py_sites(SRC, {2}) if x["label"] == "`<` -> `>=`"][0]
        out = mc.py_apply(ast.parse(SRC), s)
        self.assertIn("x >= lo", out)
        compile(out, "m.py", "exec")

    def test_print_and_logging_calls_are_not_mutated(self):
        labels = {s["label"] for s in mc.py_sites("def f():\n    print('x')\n    logging.info('y')\n", None)}
        self.assertNotIn("remove call statement", labels)

    def test_pragma_no_mutate(self):
        self.assertEqual(mc.py_sites("x = 1 + 2  # pragma: no mutate\n", None), [])

    def test_js_ignores_strings_comments_and_imports(self):
        src = ("const a = require('x > y');\n// if (a > 1) return true;\n"
               "const s = 'a == b && c';\nconst t = `${x} > 3`;\n/* a < b\n  true */\n")
        self.assertEqual(mc.js_sites(src, None), [])

    def test_js_mutations(self):
        src = ("function f(a, b) {\n  if (a >= 10 && b === 3) {\n    return a + b;\n  }\n"
               "  g(a);\n  return false;\n}\n")
        labels = {s["label"].split(" (#")[0] for s in mc.js_sites(src, None)}
        for want in ("`>=` -> `<`", "`&&` -> `||`", "`===` -> `!==`", "`+` -> `-`", "`10` -> `11`",
                     "negate `if` condition", "`return <value>` -> `return undefined`",
                     "remove call statement", "`false` -> `true`"):
            self.assertIn(want, labels)

    def test_js_generic_and_arrow_are_left_alone(self):
        src = "const f = (x: Array<number>): Promise<void> => x.length;\n"
        labels = {s["label"] for s in mc.js_sites(src, None)}
        self.assertFalse(any(l.startswith(("`<`", "`>`")) for l in labels))

    def test_is_test_file(self):
        for t in ("tests/test_x.py", "pkg/x_test.py", "src/a.test.ts", "src/__tests__/a.js",
                  "conftest.py", "tests_weak/test_pricing.py", "a.spec.jsx"):
            self.assertTrue(mc.is_test_file(t), t)
        for s in ("src/pricing.py", "lib/contest.py", "src/latest.js"):
            self.assertFalse(mc.is_test_file(s), s)


class EndToEndTests(unittest.TestCase):
    def test_weak_fails_strong_passes_and_repo_is_restored(self):
        repo = Repo(WEAK)
        try:
            before = (repo.dir / "m.py").read_bytes()
            mtime = (repo.dir / "m.py").stat().st_mtime_ns
            weak = repo.run("--json", "--max-mutants", "14")
            data = json.loads(weak.stdout)
            self.assertEqual(weak.returncode, 1, weak.stdout + weak.stderr)
            self.assertGreater(data["survived"], 3)
            self.assertLess(data["score"], 50)
            self.assertTrue(data["restored"])
            self.assertEqual((repo.dir / "m.py").read_bytes(), before)
            self.assertEqual((repo.dir / "m.py").stat().st_mtime_ns, mtime)
            (repo.dir / "tests" / "test_m.py").write_text(STRONG, encoding="utf-8")
            strong = repo.run("--json", "--max-mutants", "14")
            sdata = json.loads(strong.stdout)
            self.assertEqual(strong.returncode, 0, strong.stdout + strong.stderr)
            self.assertGreaterEqual(sdata["score"], 75)  # the rest are equivalent mutants (x <= lo returns lo anyway)
            status = subprocess.run(["git", "status", "--short"], cwd=repo.dir,
                                    capture_output=True, text=True).stdout
            self.assertEqual(sorted(l[3:] for l in status.splitlines()), ["m.py", "tests/"])
        finally:
            repo.close()

    def test_survivors_are_reported_with_file_and_line(self):
        repo = Repo(WEAK)
        try:
            r = repo.run()
            self.assertIn("SURVIVED", r.stdout)
            self.assertRegex(r.stdout, r"m\.py:\d+\s+`")
            self.assertIn("Source files restored: yes", r.stdout)
        finally:
            repo.close()

    def test_min_score_controls_exit_code(self):
        repo = Repo(WEAK)
        try:
            self.assertEqual(repo.run("--min-score", "0").returncode, 0)
            self.assertEqual(repo.run("--min-score", "100").returncode, 1)
        finally:
            repo.close()

    def test_failing_baseline_aborts_with_2_and_touches_nothing(self):
        repo = Repo("import unittest\n\n\nclass T(unittest.TestCase):\n    def test_x(self):\n        self.fail('red')\n")
        try:
            before = (repo.dir / "m.py").read_bytes()
            r = repo.run()
            self.assertEqual(r.returncode, 2)
            self.assertIn("BASELINE FAILED", r.stderr)
            self.assertEqual((repo.dir / "m.py").read_bytes(), before)
        finally:
            repo.close()

    def test_test_only_diff_has_nothing_to_mutate(self):
        repo = Repo(WEAK)
        try:
            git(repo.dir, "add", "-A")
            git(repo.dir, "commit", "-qm", "all")
            (repo.dir / "tests" / "test_m.py").write_text(STRONG, encoding="utf-8")
            r = repo.run()
            self.assertEqual(r.returncode, 0)
            self.assertIn("Nothing to mutate", r.stdout)
        finally:
            repo.close()

    def test_list_does_not_change_anything(self):
        repo = Repo(WEAK)
        try:
            before = (repo.dir / "m.py").read_bytes()
            r = repo.run("--list")
            self.assertEqual(r.returncode, 0)
            self.assertIn("m.py:2", r.stdout)
            self.assertEqual((repo.dir / "m.py").read_bytes(), before)
        finally:
            repo.close()

    def test_timeout_counts_as_killed_and_restores(self):
        d = Path(tempfile.mkdtemp(prefix="ht-timeout-"))
        try:
            (d / "m.py").write_text("X = 1\n", encoding="utf-8")
            (d / "check.py").write_text("import time, m\nif m.X != 1:\n    time.sleep(60)\n", encoding="utf-8")
            r = subprocess.run([sys.executable, str(_paths.MUTATION_CHECK), "--files", "m.py", "--cmd",
                                f'"{sys.executable}" check.py', "--timeout", "2", "--json"],
                               cwd=d, capture_output=True, text=True)
            data = json.loads(r.stdout)
            self.assertEqual(data["survived"], 0, r.stdout)
            self.assertEqual(data["score"], 100)
            self.assertEqual((d / "m.py").read_text(encoding="utf-8"), "X = 1\n")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_keyboard_interrupt_restores_the_file(self):
        repo = Repo(STRONG)
        try:
            before = (repo.dir / "m.py").read_bytes()
            calls = {"n": 0}
            real = mc.run_cmd

            def flaky(cmd, cwd, timeout):
                calls["n"] += 1
                if calls["n"] == 4:
                    raise KeyboardInterrupt
                return real(cmd, cwd, timeout)

            mc.run_cmd = flaky
            try:
                rc = mc.main(["--cmd", CMD, "--project", str(repo.dir), "--json"])
            finally:
                mc.run_cmd = real
            self.assertEqual(rc, 130)
            self.assertEqual((repo.dir / "m.py").read_bytes(), before)
        finally:
            repo.close()

    def test_guard_restores_after_a_crash(self):
        d = Path(tempfile.mkdtemp(prefix="ht-guard-"))
        try:
            f = d / "a.py"
            f.write_text("x = 1\n", encoding="utf-8")
            g = mc.Guard()
            g.snapshot(f)
            g.write(f, "x = 2\n", 1)
            self.assertEqual(f.read_text(encoding="utf-8"), "x = 2\n")
            self.assertTrue(g.restore_all())
            self.assertEqual(f.read_text(encoding="utf-8"), "x = 1\n")
        finally:
            shutil.rmtree(d, ignore_errors=True)


@unittest.skipUnless(shutil.which("node"), "node not installed")
class JavaScriptTests(unittest.TestCase):
    def test_js_weak_vs_strong(self):
        d = Path(tempfile.mkdtemp(prefix="ht-js-"))
        try:
            (d / "calc.js").write_text(
                "function add(a, b) {\n  if (a > 100) {\n    return 100;\n  }\n  return a + b;\n}\n"
                "module.exports = { add };\n", encoding="utf-8")
            weak = "const assert = require('node:assert');\nconst { add } = require('./calc');\nassert.ok(add(1, 2));\n"
            strong = ("const assert = require('node:assert');\nconst { add } = require('./calc');\n"
                      "assert.strictEqual(add(1, 2), 3);\nassert.strictEqual(add(100, 1), 101);\n"
                      "assert.strictEqual(add(101, 1), 100);\nassert.strictEqual(add(0, 0), 0);\n")

            def run():
                r = subprocess.run([sys.executable, str(_paths.MUTATION_CHECK), "--files", "calc.js",
                                    "--cmd", "node t.js", "--json"], cwd=d, capture_output=True, text=True)
                return json.loads(r.stdout)

            (d / "t.js").write_text(weak, encoding="utf-8")
            self.assertLess(run()["score"], 50)
            (d / "t.js").write_text(strong, encoding="utf-8")
            s = run()
            self.assertEqual(s["survived"], 0, s)
            self.assertTrue(s["restored"])
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
