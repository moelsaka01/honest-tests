"""Tests that kill the survivors found by running mutation_check.py on itself (full suite as the test command)."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

import _paths
from test_mutation_check import SRC, STRONG, Repo, git

mc = _paths.load(_paths.MUTATION_CHECK, "mutation_check")


def labels(src, line=None, ts=False):
    return [s["label"] for s in mc.js_sites(src, None, ts) if line is None or s["line"] == line]


def edit_results(src, label, line=1):
    out = []
    for s in mc.js_sites(src, None):
        if s["line"] == line and s["label"] == label:
            out.append(mc.js_apply(src, s))
    return out


class JsLexerGapTests(unittest.TestCase):
    def test_a_newline_ends_the_statement_so_each_call_is_removable(self):
        self.assertEqual(labels("foo()\nbar(1)\n"),
                         ["remove call statement", "remove call statement", "`1` -> `2`", "`1` -> `0`"])

    def test_block_comment_end_is_found_even_when_it_is_empty(self):
        self.assertEqual(labels("a /**/ b = 1 + 2\n"),
                         ["`1` -> `2`", "`1` -> `0`", "`+` -> `-`", "`2` -> `3`", "`2` -> `1`"])

    def test_await_call_statements(self):
        self.assertEqual(labels("await foo.bar(1)\nawait baz()\n"),
                         ["remove call statement", "`1` -> `2`", "`1` -> `0`", "remove call statement"])
        removed = edit_results("await foo.bar(1)\nawait baz()\n", "remove call statement", 1)
        self.assertEqual(len(removed), 1)
        self.assertNotIn("foo.bar", removed[0])
        self.assertIn("await baz()", removed[0])

    def test_unary_minus_is_not_a_binary_operator_but_binary_minus_is(self):
        self.assertEqual(labels("x = -1 + 2\nz = a - -1\n", 1),
                         ["`1` -> `2`", "`1` -> `0`", "`+` -> `-`", "`2` -> `3`", "`2` -> `1`"])
        self.assertIn("`-` -> `+`", labels("x = -1 + 2\nz = a - -1\n", 2))

    def test_decrementing_one_gives_a_plain_zero_but_zero_gives_a_parenthesised_negative(self):
        self.assertEqual(edit_results("let a = 1\n", "`1` -> `0`"), ["let a = 0\n"])
        self.assertEqual(edit_results("let a = 0\n", "`0` -> `-1`"), ["let a = (-1)\n"])
        self.assertEqual(edit_results("let a = 2\n", "`2` -> `1`"), ["let a = 1\n"])

    def test_float_literals_are_bumped_by_one(self):
        self.assertEqual(labels("x = 0.5 + 1.0\n"), ["`0.5` -> `1.5`", "`+` -> `-`", "`1.0` -> `2.0`"])
        self.assertEqual(edit_results("x = 0.5\n", "`0.5` -> `1.5`"), ["x = 1.5\n"])

    def test_true_and_false_flip_but_property_names_do_not(self):
        self.assertEqual(labels("x = true && false\nobj.true = 1\n", 1),
                         ["`true` -> `false`", "`&&` -> `||`", "`false` -> `true`"])
        self.assertEqual(edit_results("x = true\n", "`true` -> `false`"), ["x = false\n"])
        self.assertEqual(edit_results("x = false\n", "`false` -> `true`"), ["x = true\n"])
        self.assertEqual(labels("x = true && false\nobj.true = 1\n", 2), ["`1` -> `2`", "`1` -> `0`"])

    def test_two_identical_labels_on_one_line_are_numbered(self):
        self.assertEqual(labels("x = y\n/ 2 / z\n", 2),
                         ["`/` -> `*` (#1)", "`2` -> `3`", "`2` -> `1`", "`/` -> `*` (#2)"])
        self.assertEqual(labels("x = y / 2\n", 1), ["`/` -> `*`", "`2` -> `3`", "`2` -> `1`"])

    def test_dynamic_import_is_code_but_static_import_is_not(self):
        self.assertEqual(labels("import(a + 1)\n"), ["`+` -> `-`", "`1` -> `2`", "`1` -> `0`"])
        self.assertEqual(labels("import m from 'm'\nx = 1\n", 1), [])
        self.assertEqual(labels("import.meta.x + 1\n"), ["`+` -> `-`", "`1` -> `2`", "`1` -> `0`"])

    def test_empty_regex_is_not_a_regex(self):
        self.assertEqual(mc._regex_end("//", 0), 0)
        self.assertEqual(mc._regex_end("/a/g", 0), 4)
        self.assertEqual(mc._regex_end("/a", 0), 0)

    def test_only_a_template_that_opens_with_a_backtick_continues_a_line(self):
        T = types.SimpleNamespace
        self.assertTrue(mc._starts_cont(T(k="tpl", t="`x`")))
        self.assertFalse(mc._starts_cont(T(k="tpl", t="}x`")))
        self.assertFalse(mc._starts_cont(T(k="str", t="`")))


class GitBaseTests(unittest.TestCase):
    def test_base_branch_is_compared_at_the_merge_base_not_its_tip(self):
        d = Path(tempfile.mkdtemp(prefix="ht-base-"))
        try:
            git(d, "init", "-q")
            git(d, "config", "user.email", "t@example.com")
            git(d, "config", "user.name", "t")
            (d / "m.py").write_text("a = 1\nb = 2\n", encoding="utf-8")
            git(d, "add", "-A")
            git(d, "commit", "-qm", "base")
            git(d, "branch", "-M", "main")
            git(d, "checkout", "-q", "-b", "feature")
            git(d, "checkout", "-q", "main")
            (d / "m.py").write_text("a = 1\nb = 3\n", encoding="utf-8")   # main moves on
            git(d, "commit", "-qam", "main moves")
            git(d, "checkout", "-q", "feature")
            (d / "m.py").write_text("a = 5\nb = 2\n", encoding="utf-8")   # feature edits line 1 only
            self.assertEqual(mc.collect_changed(d, "main"), {"m.py": {1}})
        finally:
            shutil.rmtree(d, ignore_errors=True)


class DetectCmdAndPidTests(unittest.TestCase):
    def test_python_tests_alone_are_enough_to_detect_a_command(self):
        d = Path(tempfile.mkdtemp(prefix="ht-detect-"))
        try:
            self.assertIsNone(mc.detect_cmd(d))
            (d / "test_x.py").write_text("", encoding="utf-8")
            self.assertIn("-m ", mc.detect_cmd(d))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_pytest_ini_alone_is_enough_too(self):
        d = Path(tempfile.mkdtemp(prefix="ht-detect-"))
        try:
            (d / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
            self.assertIn("-m ", mc.detect_cmd(d))
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_pid_alive_is_true_for_a_running_process_and_false_after_it_exits(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            self.assertTrue(mc.pid_alive(child.pid))
            self.assertTrue(mc.pid_alive(os.getpid()))
        finally:
            child.kill()
            child.wait()
        self.assertFalse(mc.pid_alive(child.pid))


class WorkdirTests(unittest.TestCase):
    def test_copy_skips_caches_and_git_but_keeps_sources(self):
        root = Path(tempfile.mkdtemp(prefix="ht-wd-"))
        try:
            proj = root / "proj"
            (proj / ".git").mkdir(parents=True)
            (proj / "__pycache__").mkdir()
            (proj / "pkg").mkdir()
            (proj / ".git" / "HEAD").write_text("x", encoding="utf-8")
            (proj / "__pycache__" / "a.pyc").write_text("x", encoding="utf-8")
            (proj / "pkg" / "a.py").write_text("x = 1\n", encoding="utf-8")
            (proj / "b.py").write_text("y = 2\n", encoding="utf-8")
            dst = mc.make_workdir(proj, root, "w0")
            self.assertEqual(sorted(p.name for p in dst.iterdir()), ["b.py", "pkg"])
            self.assertEqual((dst / "pkg" / "a.py").read_text(encoding="utf-8"), "x = 1\n")
        finally:
            shutil.rmtree(root, ignore_errors=True)


class CliGapTests(unittest.TestCase):
    def cli(self, *args, env=None, cwd=None):
        e = dict(os.environ)
        e.update(env or {})
        return subprocess.run([sys.executable, str(_paths.MUTATION_CHECK), *args], cwd=cwd,
                              capture_output=True, text=True, env=e)

    def test_restore_with_nothing_to_restore_exits_zero(self):
        r = self.cli("--restore")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.strip(), "Nothing to restore.")

    def test_project_that_is_not_a_directory_exits_two(self):
        r = self.cli("--project", str(_paths.MUTATION_CHECK))
        self.assertEqual(r.returncode, 2)
        self.assertIn("is not a directory", r.stderr)

    def test_header_scoring_and_threshold_boundary(self):
        repo = Repo(STRONG)
        try:
            text = repo.run()
            self.assertIn("honest-tests / mutation-check", text.stdout)
            data = json.loads(repo.run("--json").stdout)
            score = data["score"]
            self.assertIsNotNone(score)
            on_the_line = repo.run("--min-score", str(score))
            self.assertEqual(on_the_line.returncode, 0)   # exactly on the line passes
            self.assertIn("-> PASS", on_the_line.stdout)
            if score < 100:
                self.assertEqual(repo.run("--min-score", str(score + 1)).returncode, 1)
        finally:
            repo.close()

    @unittest.skipIf(sys.version_info < (3, 10), "Python 3.9 keeps unreachable code, so the dead-code mutant differs")
    def test_provably_equivalent_mutants_are_reported_as_not_counted(self):
        repo = Repo(STRONG)
        try:
            (repo.dir / "m.py").write_text(SRC + "\n\ndef dead(a):\n    return a\n    z = 5\n", encoding="utf-8")
            out = repo.run().stdout
            self.assertIn("Not counted: ", out)
            self.assertIn("identical-bytecode (provably equivalent)", out)
        finally:
            repo.close()

    def test_survivor_rows_are_cut_to_the_terminal_width_and_killed_ones_need_verbose(self):
        repo = Repo("import unittest\nimport m\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n"
                    "        self.assertIsNotNone(m.long_name(1, 2))\n")
        try:
            (repo.dir / "m.py").write_text(
                "def long_name(argument_one, argument_two):\n"
                "    return argument_one + argument_two + argument_one + argument_two\n", encoding="utf-8")
            quiet = subprocess.run([sys.executable, str(_paths.MUTATION_CHECK), "--cmd",
                                    f'"{sys.executable}" -m unittest discover -s tests', "--show", "0"],
                                   cwd=repo.dir, capture_output=True, text=True, env={**os.environ, "COLUMNS": "30"})
            rows = [ln for ln in quiet.stdout.splitlines() if ln.startswith("  m.py:")]
            self.assertTrue(rows, quiet.stdout)
            for ln in rows:
                source = ln.split(" | ", 1)[1]
                self.assertLessEqual(len(source), 20, ln)
            self.assertTrue(any(ln.endswith("...") and len(ln.split(" | ", 1)[1]) == 20 for ln in rows), rows)
            self.assertNotIn("KILLED", quiet.stdout)
        finally:
            repo.close()

    def test_verbose_lists_killed_mutants_only_when_there_are_some(self):
        strong = Repo(STRONG)
        weak_none = Repo("import unittest\nimport m\n\n\nclass T(unittest.TestCase):\n    def test_a(self):\n"
                         "        self.assertTrue(True)\n")
        try:
            self.assertNotIn("KILLED", strong.run().stdout)
            self.assertIn("KILLED (", strong.run("--verbose").stdout)
            out = weak_none.run("--verbose").stdout
            self.assertNotIn("KILLED", out)
        finally:
            strong.close()
            weak_none.close()


@unittest.skipUnless(shutil.which("node"), "node not installed")
class NodeCheckGapTests(unittest.TestCase):
    def test_a_mutant_that_is_not_valid_javascript_is_reported_invalid_not_survived(self):
        d = Path(tempfile.mkdtemp(prefix="ht-node-"))
        try:
            git(d, "init", "-q")
            git(d, "config", "user.email", "t@example.com")
            git(d, "config", "user.name", "t")
            (d / "m.js").write_text("x\n", encoding="utf-8")
            git(d, "add", "-A")
            git(d, "commit", "-qm", "base")
            (d / "m.js").write_text("module.exports = 1 + 2;\n", encoding="utf-8")
            site = {"line": 1, "label": "broken", "kind": "edits", "edits": [(0, 6, "module.exports = = =")]}
            buf = io.StringIO()
            with mock.patch.object(mc, "js_sites", return_value=[site]), contextlib.redirect_stdout(buf):
                code = mc.main(["--project", str(d), "--cmd", 'node -e "0"', "--json", "--min-score", "0"])
            self.assertEqual(code, 0)
            data = json.loads(buf.getvalue())
            self.assertEqual((data["invalid"], data["survived"], data["killed"]), (1, 0, 0))
            self.assertEqual((d / "m.js").read_text(encoding="utf-8"), "module.exports = 1 + 2;\n")
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
