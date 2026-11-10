"""More tests that kill survivors of mutation_check.py run on itself (second, larger sample: 240 of 1,762 mutants)."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest import mock

import _paths
from test_mutation_check import SRC, STRONG, WEAK, Repo, git

mc = _paths.load(_paths.MUTATION_CHECK, "mutation_check")


class FileSelectionTests(unittest.TestCase):
    def test_is_test_file_answers_with_real_booleans(self):
        self.assertIs(mc.is_test_file("src/pricing.py"), False)
        self.assertIs(mc.is_test_file("tests/test_x.py"), True)
        self.assertIs(mc.is_test_file("conftest.py"), True)
        self.assertIs(mc.is_test_file("a.spec.jsx"), True)


def texts(src):
    return [t.t for t in mc.js_lex(src)]


def kinds(src):
    return [t.k for t in mc.js_lex(src)]


def js_labels(src, line=None, ts=False):
    return [s["label"] for s in mc.js_sites(src, None, ts) if line is None or s["line"] == line]


class GitGapTests(unittest.TestCase):
    def setUp(self):
        self.repo = Repo(STRONG)
        self.addCleanup(self.repo.close)

    def test_an_unknown_base_is_named_in_the_error(self):
        with self.assertRaises(SystemExit) as cm:
            mc.collect_changed(self.repo.dir, "no-such-ref")
        self.assertEqual(str(cm.exception), "error: unknown --base ref 'no-such-ref'")

    def test_a_base_with_no_common_history_is_diffed_directly(self):
        d = self.repo.dir
        git(d, "add", "-A")
        git(d, "commit", "-qm", "work")
        git(d, "checkout", "-q", "--orphan", "island")
        (d / "m.py").write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")
        git(d, "add", "-A")
        git(d, "commit", "-qm", "unrelated")
        (d / "m.py").write_text("a = 1\nb = 2\nc = 4\n", encoding="utf-8")
        first = subprocess.run(["git", "rev-list", "--max-parents=0", "--all"], cwd=d, capture_output=True,
                               text=True).stdout.split()
        other = [c for c in first if c != subprocess.run(["git", "rev-parse", "HEAD"], cwd=d, capture_output=True,
                                                         text=True).stdout.strip()][0]
        changed = mc.collect_changed(d, other)   # the stub commit: m.py was two other lines there
        self.assertEqual(changed["m.py"], {1, 2, 3})


class PythonMutatorGapTests(unittest.TestCase):
    def test_a_float_constant_is_bumped_by_exactly_one(self):
        sites = mc.py_sites("x = 0.5\n", None)
        self.assertEqual([(s["label"], s["value"]) for s in sites], [("`0.5` -> `1.5`", 1.5)])
        import ast
        self.assertEqual(mc.py_apply(ast.parse("x = 0.5\n"), sites[0]), "x = 1.5\n")


class JsLexerGapTests2(unittest.TestCase):
    def test_a_slash_after_a_number_or_string_is_division_not_a_regex(self):
        self.assertEqual(kinds("x = 1 / 2 / 3"), ["id", "p", "num", "p", "num", "p", "num"])
        self.assertEqual(kinds("x = 'a' / 2 / 3"), ["id", "p", "str", "p", "num", "p", "num"])

    def test_a_slash_inside_a_template_substitution_starts_a_regex_but_not_after_the_template(self):
        self.assertEqual(texts("`${/a+/.test(s)}`"), ["`${", "/a+/", ".", "test", "(", "s", ")", "}`"])
        self.assertEqual(kinds("`a` / 2 / 3"), ["tpl", "p", "num", "p", "num"])

    def test_the_token_right_after_a_block_comment_is_kept(self):
        self.assertEqual(texts("x = 1 /**/+ 2\n"), ["x", "=", "1", "+", "2"])
        self.assertEqual(texts("x = 1 /* c */+ 2\n"), ["x", "=", "1", "+", "2"])

    def test_an_unterminated_string_at_the_end_of_the_file_is_one_token_not_a_crash(self):
        self.assertEqual(texts("x = 'abc"), ["x", "=", "'abc"])
        self.assertEqual(texts('x = "abc'), ["x", "=", '"abc'])

    def test_a_template_with_two_substitutions(self):
        self.assertEqual(texts("`${a}-${b}` + 1"), ["`${", "a", "}-${", "b", "}`", "+", "1"])
        self.assertEqual(kinds("`${a}-${b}` + 1"), ["tpl", "id", "tpl", "id", "tpl", "p", "num"])

    def test_a_dot_is_a_number_only_when_a_digit_follows_it(self):
        self.assertEqual(texts("a."), ["a", "."])                 # a dot at the very end must not read past it
        self.assertEqual(texts("a1.b"), ["a1", ".", "b"])         # the digit BEFORE the dot does not count
        self.assertEqual(texts("x = .5"), ["x", "=", ".5"])
        self.assertEqual(kinds("x = .5"), ["id", "p", "num"])

    def test_optional_chaining_versus_a_ternary_with_a_decimal(self):
        self.assertEqual(texts("a?."), ["a", "?."])               # at the very end: no read past the source
        self.assertEqual(texts("a?.b"), ["a", "?.", "b"])
        self.assertEqual(texts("a?.5:1"), ["a", "?", ".5", ":", "1"])

    def test_the_shortest_regex_is_one_character_long(self):
        self.assertEqual(mc._regex_end("/a/", 0), 3)
        self.assertEqual(texts("x = /a/\n"), ["x", "=", "/a/"])


def mutate(src, label, line=1):
    """The source after the one mutant with this label on this line."""
    hits = [s for s in mc.js_sites(src, None) if s["line"] == line and s["label"] == label]
    assert len(hits) == 1, [s["label"] for s in mc.js_sites(src, None)]
    return mc.js_apply(src, hits[0])


class JsExpressionEndTests(unittest.TestCase):
    def test_a_returned_expression_runs_to_the_semicolon_not_to_the_first_operator(self):
        self.assertEqual(mutate("function f(){ return a + b; }\n", "`return <value>` -> `return undefined`"),
                         "function f(){ return undefined; }\n")

    def test_an_arrow_body_ends_where_a_semicolon_would_be_inserted(self):
        self.assertEqual(mutate("const f = () => a\nb + 1\n", "arrow body -> `undefined`"),
                         "const f = () => undefined\nb + 1\n")
        self.assertEqual(mutate("const f = () => a\n+ b\nc()\n", "arrow body -> `undefined`"),
                         "const f = () => undefined\nc()\n")


class JsStatementTests(unittest.TestCase):
    def test_a_call_after_a_case_label_is_a_statement(self):
        self.assertEqual(js_labels("case 1: foo()\n"),      # also when the snippet starts with `case`
                         ["`1` -> `2`", "`1` -> `0`", "remove call statement"])
        self.assertEqual(js_labels("switch (x) { case f(a ? 1 : 2): foo() }\n"),   # brackets in the label are skipped
                         ["`1` -> `2`", "`1` -> `0`", "`2` -> `3`", "`2` -> `1`", "remove call statement"])

    def test_a_call_on_a_continuation_line_is_not_a_statement(self):
        self.assertEqual(js_labels("const x =\n  foo(1)\n"), ["`1` -> `2`", "`1` -> `0`"])

    def test_a_source_that_stops_in_the_middle_of_a_statement_does_not_crash(self):
        for tail in ("foo", "foo.", "await"):
            self.assertEqual(js_labels("x = 1\n" + tail), ["`1` -> `2`", "`1` -> `0`"], tail)

    def test_a_dynamic_import_after_another_statement_is_still_code(self):
        self.assertEqual(js_labels("x = 1;\nimport(a + 1)\n", 2), ["`+` -> `-`", "`1` -> `2`", "`1` -> `0`"])
        self.assertEqual(js_labels("x = 1;\nimport a from 'a'\ny = 2\n", 2), [])

    def test_a_call_is_only_removed_when_the_statement_ends_right_after_it(self):
        self.assertEqual(js_labels("foo()`x`\n"), [])              # a tagged template continues the expression
        self.assertEqual(js_labels("bar(); foo() + 1"), ["remove call statement", "`+` -> `-`", "`1` -> `2`", "`1` -> `0`"])
        self.assertEqual(mutate("foo();", "remove call statement"), ";")
        self.assertEqual(mutate("foo()\n", "remove call statement"), ";\n")

    def test_only_the_increments_inside_a_for_header_are_left_alone(self):
        self.assertEqual(js_labels("for (let i = 0; i < n; i++) {}\n"),
                         ["`0` -> `1`", "`0` -> `-1`", "`<` -> `>=`", "`<` -> `<=`"])
        self.assertEqual(js_labels("x++\nfor (;;) {}\n"), ["`++` -> `--`"])     # just before the `for`
        self.assertEqual(js_labels("for (;;) ++x\n"), ["`++` -> `--`"])         # just after the header
        self.assertEqual(js_labels("for (++i;;) {}\n"), [])

    def test_an_if_without_a_condition_is_not_negated(self):
        self.assertEqual(js_labels("if () {}\n"), [])
        self.assertEqual(mutate("if (a) {}\n", "negate `if` condition"), "if (!(a)) {}\n")

    def test_an_arrow_body_of_several_tokens_is_replaced_whole(self):
        self.assertEqual(mutate("const f = x => x * y;\n", "arrow body -> `undefined`"), "const f = x => undefined;\n")
        self.assertEqual(mutate("const f = x => y;\n", "arrow body -> `undefined`"), "const f = x => undefined;\n")


class TestOutputTests(unittest.TestCase):
    def test_the_number_of_tests_is_read_from_each_runner_s_summary(self):
        self.assertEqual(mc.count_tests("Ran 3 tests in 0.1s"), 3)                           # unittest
        self.assertEqual(mc.count_tests("Ran 1 test in 0.1s"), 1)
        self.assertEqual(mc.count_tests("==== 5 passed in 1s ===="), 5)                      # pytest
        self.assertEqual(mc.count_tests("# pass 4"), 4)                                      # node --test
        self.assertEqual(mc.count_tests("Tests:       1 failed, 6 passed, 7 total"), 6)      # jest
        self.assertEqual(mc.count_tests("  7 passing (3ms)"), 7)                             # mocha
        self.assertIsNone(mc.count_tests("no summary here"))


class DetectCmdGapTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="ht-detect2-"))
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def test_a_package_json_test_script_means_npm_test(self):
        (self.d / "package.json").write_text('{"scripts": {"test": "node --test"}}', encoding="utf-8")
        self.assertEqual(mc.detect_cmd(self.d), "npm test --silent")

    def test_the_npm_placeholder_script_and_a_broken_package_json_are_not_a_command(self):
        (self.d / "package.json").write_text(
            '{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}', encoding="utf-8")
        self.assertIsNone(mc.detect_cmd(self.d))
        (self.d / "package.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(mc.detect_cmd(self.d))
        (self.d / "package.json").write_text('{"name": "x"}', encoding="utf-8")
        self.assertIsNone(mc.detect_cmd(self.d))


class PidAliveGapTests(unittest.TestCase):
    def test_a_pid_that_does_not_exist_is_exactly_false(self):
        self.assertIs(mc.pid_alive(2**31 - 16), False)
        self.assertIs(mc.pid_alive(os.getpid()), True)

    def test_the_posix_probe_sends_signal_zero_and_reads_the_three_outcomes(self):
        # runs on every platform: the POSIX branch is driven with a stand-in for os.kill
        for outcome, expected in ((None, True), (ProcessLookupError(), False), (PermissionError(), True)):
            calls = []

            def fake_kill(pid, sig, outcome=outcome):
                calls.append((pid, sig))
                if outcome is not None:
                    raise outcome

            with mock.patch.object(mc.os, "name", "posix"), mock.patch.object(mc.os, "kill", fake_kill, create=True):
                result = mc.pid_alive(4242)
            self.assertIs(result, expected, repr(outcome))
            self.assertEqual(calls, [(4242, 0)])


class GuardGapTests(unittest.TestCase):
    def test_every_snapshot_gets_its_own_backup_file(self):
        d = Path(tempfile.mkdtemp(prefix="ht-guard2-"))
        g = mc.Guard()
        try:
            (d / "a.py").write_bytes(b"a = 1\n")
            (d / "b.py").write_bytes(b"b = 2\n")
            self.assertEqual(g.snapshot(d / "a.py"), b"a = 1\n")
            self.assertEqual(g.snapshot(d / "b.py"), b"b = 2\n")
            man = json.loads((g.tmp / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(man["pid"], os.getpid())
            by_path = {e["path"]: name for name, e in man["files"].items()}
            self.assertEqual(sorted(by_path), [str(d / "a.py"), str(d / "b.py")])
            self.assertEqual((g.tmp / by_path[str(d / "a.py")]).read_bytes(), b"a = 1\n")
            self.assertEqual((g.tmp / by_path[str(d / "b.py")]).read_bytes(), b"b = 2\n")
        finally:
            self.assertTrue(g.restore_all())
            shutil.rmtree(d, ignore_errors=True)
        self.assertFalse(g.tmp.exists())


NODE = shutil.which("node")
TSJS_PATH = mc.find_typescript(_paths.ROOT) if NODE else None


@unittest.skipUnless(TSJS_PATH, "typescript is not installed")
class AstralRangeTests(unittest.TestCase):
    """TypeScript counts UTF-16 units, Python counts code points: an emoji is two units but one character."""

    def ranges(self, src):
        d = Path(tempfile.mkdtemp(prefix="ht-astral-"))
        try:
            (d / "a.ts").write_bytes(src.encode("utf-8"))
            return mc.ts_exclusions(NODE, TSJS_PATH, d / "a.ts", src)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_ranges_after_emoji_point_at_the_type_exactly(self):
        src = 'const s = "\U0001F600\U0001F600"; let x: number = 1;\n'
        at = src.index("number")
        self.assertEqual(self.ranges(src), [(at, at + 6)])

    def test_a_range_that_ends_at_the_end_of_the_source(self):
        src = 'const s = "\U0001F600\U0001F600"; let x: number'
        self.assertEqual(self.ranges(src), [(len(src) - 6, len(src))])

    def test_without_emoji_the_ranges_are_passed_through(self):
        self.assertEqual(self.ranges("let x: number"), [(7, 13)])


class WorkdirGapTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="ht-wd2-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def test_removing_a_copy_never_empties_the_linked_node_modules(self):
        proj = self.root / "proj"
        (proj / "node_modules" / "pkg").mkdir(parents=True)
        (proj / "node_modules" / "pkg" / "x.js").write_text("1\n", encoding="utf-8")
        (proj / "a.py").write_text("a = 1\n", encoding="utf-8")
        wd = mc.make_workdir(proj, self.root, "w0")
        self.assertEqual((wd / "node_modules" / "pkg" / "x.js").read_text(encoding="utf-8"), "1\n")
        mc.remove_workdir(wd)
        self.assertFalse(wd.exists())
        self.assertEqual((proj / "node_modules" / "pkg" / "x.js").read_text(encoding="utf-8"), "1\n")

    def test_a_link_that_cannot_be_unlinked_is_removed_as_a_directory(self):
        wd = self.root / "w1"
        (wd / "node_modules").mkdir(parents=True)      # a plain directory: os.unlink refuses it, like an old junction
        with mock.patch.object(mc.shutil, "rmtree") as rmtree:
            mc.remove_workdir(wd)
        self.assertFalse((wd / "node_modules").exists())
        rmtree.assert_called_once_with(wd, ignore_errors=True)


@unittest.skipUnless(os.name == "nt", "Windows console control handler")
class ConsoleHandlerGapTests(unittest.TestCase):
    def test_the_handler_restores_and_then_reports_not_handled_so_windows_still_ends_the_process(self):
        with mock.patch.object(mc, "_on_console_event", return_value=True) as on_event:
            mc.install_console_close_handler()
            self.assertEqual(mc._console_cb(2), 0)
        on_event.assert_called_once_with(2)


DEAD_PID = 2**31 - 16


class RestoreStaleTests(unittest.TestCase):
    """`--restore` after a run that died: what is put back, what is counted, and the timestamps."""

    def setUp(self):
        self.proj = Path(tempfile.mkdtemp(prefix="ht-stale-"))
        self.addCleanup(shutil.rmtree, self.proj, ignore_errors=True)
        self.bak = Path(tempfile.mkdtemp(prefix=mc.BACKUP_PREFIX))
        self.addCleanup(shutil.rmtree, self.bak, ignore_errors=True)
        self.stamp = 1_600_000_000 * 10**9

    def backup(self, name, original):
        target = self.proj / name
        n = len(list(self.bak.glob("*.bak"))) + 1
        (self.bak / f"{n}.bak").write_bytes(original)
        man_file = self.bak / "manifest.json"
        man = json.loads(man_file.read_text(encoding="utf-8")) if man_file.exists() else {"pid": DEAD_PID, "files": {}}
        man["files"][f"{n}.bak"] = {"path": str(target), "atime_ns": self.stamp, "mtime_ns": self.stamp}
        man_file.write_text(json.dumps(man), encoding="utf-8")
        return target

    def restore(self):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            n = mc.restore_stale(self.proj)
        return n, out.getvalue()

    def test_a_mutated_file_is_put_back_counted_and_gets_its_old_timestamp(self):
        target = self.backup("m.py", b"x = 1\n")
        target.write_bytes(b"x = 2\n")
        n, out = self.restore()
        self.assertEqual(n, 1)
        self.assertEqual(out, f"restored {target}\n")
        self.assertEqual(target.read_bytes(), b"x = 1\n")
        self.assertEqual(target.stat().st_mtime_ns, self.stamp)
        self.assertFalse(self.bak.exists())

    def test_a_file_that_is_already_the_original_is_not_counted(self):
        target = self.backup("m.py", b"x = 1\n")
        target.write_bytes(b"x = 1\n")
        n, out = self.restore()
        self.assertEqual((n, out), (0, ""))
        self.assertEqual(target.stat().st_mtime_ns, self.stamp)   # the timestamp is still put back
        self.assertFalse(self.bak.exists())

    def test_a_deleted_file_is_recreated_and_counted(self):
        target = self.backup("m.py", b"x = 1\n")
        gone = self.backup("gone.py", b"y = 1\n")
        target.write_bytes(b"x = 1\n")
        n, out = self.restore()
        self.assertEqual(n, 1)
        self.assertEqual(gone.read_bytes(), b"y = 1\n")
        self.assertEqual(out, f"restored {gone}\n")


PASS_CMD = f'"{sys.executable}" -c "pass"'


def run_main(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = mc.main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


class CliGapTests2(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="ht-cli2-")).resolve()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        (self.d / "m.py").write_text(SRC, encoding="utf-8")

    def listed(self, *extra):
        code, out, err = run_main("--project", self.d, "--files", "m.py", "--cmd", PASS_CMD, "--list", *extra)
        self.assertEqual(code, 0, err)
        return out

    def test_sampling_is_reproducible_and_the_default_seed_is_zero(self):
        import random
        sites = mc.py_sites(SRC, None)
        self.assertGreater(len(sites), 5)

        lines = SRC.split("\n")

        def expected(seed):
            rows = sorted((s["line"], s["label"]) for s in random.Random(seed).sample(sites, 3))
            return [f"  m.py:{line}  {label:<34}  {lines[line - 1].strip()}" for line, label in rows]

        def got(*extra):
            out = self.listed("--max-mutants", "3", *extra)
            self.assertIn(f"  mutants : 3 (sampled from {len(sites)}; raise --max-mutants for all)", out)
            return [ln for ln in out.splitlines() if ln.startswith("  m.py:")]

        self.assertEqual(got(), expected(0))
        self.assertEqual(got("--seed", "0"), expected(0))
        self.assertEqual(got("--seed", "7"), expected(7))
        self.assertNotEqual(expected(0), expected(7))

    def test_the_header_names_the_project_or_a_dot_for_the_current_directory(self):
        self.assertIn(f"  project : {self.d}\n", self.listed())
        here = subprocess.run([sys.executable, str(_paths.MUTATION_CHECK), "--files", "m.py", "--cmd", PASS_CMD,
                               "--list"], cwd=self.d, capture_output=True, text=True)
        self.assertEqual(here.returncode, 0, here.stderr)
        self.assertIn("  project : .\n", here.stdout)

    def test_output_survives_a_console_that_cannot_encode_the_source(self):
        (self.d / "m.py").write_text("def f(a):\n    return a + 1  # café\n", encoding="utf-8")
        r = subprocess.run([sys.executable, str(_paths.MUTATION_CHECK), "--files", "m.py", "--cmd", PASS_CMD,
                            "--list"], cwd=self.d, capture_output=True, env={**os.environ, "PYTHONIOENCODING": "ascii"})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(b"return a + 1  # caf?", r.stdout)

    def test_nothing_to_mutate_is_json_only_when_json_was_asked_for(self):
        git(self.d, "init", "-q")
        git(self.d, "config", "user.email", "t@example.com")
        git(self.d, "config", "user.name", "t")
        git(self.d, "add", "-A")
        git(self.d, "commit", "-qm", "base")
        code, out, _ = run_main("--project", self.d, "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), {"score": None, "mutants": [], "note": "no changed source lines"})
        code, out, _ = run_main("--project", self.d)
        self.assertEqual(code, 0)
        self.assertIn("Nothing to mutate: no changed source lines", out)
        self.assertNotIn("{", out)

    def test_the_timeout_option_also_limits_the_baseline_run(self):
        slow = f'"{sys.executable}" -c "import time; time.sleep(8)"'
        code, out, err = run_main("--project", self.d, "--files", "m.py", "--cmd", slow, "--timeout", "1")
        self.assertEqual(code, 2)
        self.assertIn("BASELINE FAILED", err)
        self.assertNotIn("mutants :", out)

    def test_the_full_list_is_in_line_order_and_is_not_called_a_sample(self):
        sites = mc.py_sites(SRC, None)
        lines = SRC.split("\n")
        out = self.listed()
        self.assertIn(f"  mutants : {len(sites)}\n", out)
        self.assertEqual([ln for ln in out.splitlines() if ln.startswith("  m.py:")],
                         [f"  m.py:{line}  {label:<34}  {lines[line - 1].strip()}"
                          for line, label in sorted((s["line"], s["label"]) for s in sites)])

    def test_a_last_line_without_a_newline_still_shows_its_source(self):
        (self.d / "m.py").write_text("x = 1 + 2", encoding="utf-8")
        rows = [ln for ln in self.listed().splitlines() if ln.startswith("  m.py:")]
        self.assertEqual(rows[0], f"  m.py:1  {'`+` -> `-`':<34}  x = 1 + 2")
        self.assertEqual(len(rows), 5)

    def test_the_baseline_line_reports_the_number_of_tests_only_when_it_is_known(self):
        counted = f'"{sys.executable}" -c "print(\'Ran 3 tests in 0.0s\')"'
        code, out, err = run_main("--project", self.d, "--files", "m.py", "--cmd", counted, "--list")
        self.assertEqual(code, 0, err)
        self.assertRegex(out, r"\n  baseline: PASS, 3 tests in \d+\.\ds \(mutant timeout 10s\)\n")
        self.assertRegex(self.listed(), r"\n  baseline: PASS in \d+\.\ds \(mutant timeout 10s\)\n")

    def test_typescript_without_any_parser_says_it_is_guessing(self):
        (self.d / "m.tsx").write_text("export const A = (n: number) => n + 1;\n", encoding="utf-8")
        with mock.patch.object(mc, "find_typescript", return_value=None):
            code, out, err = run_main("--project", self.d, "--files", "m.tsx", "--cmd", PASS_CMD, "--list")
        self.assertEqual(code, 0, err)
        self.assertIn("\n  ts parse: built-in heuristics (no TypeScript parser available)\n", out)


class Tty(io.StringIO):
    def isatty(self):
        return True


class RunGapTests(unittest.TestCase):
    """Whole runs inside this process, on a project small enough to take a second or two."""

    CHECK = f'"{sys.executable}" -c "import m; assert m.f(1) == 2"'

    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="ht-run2-")).resolve()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)
        (self.d / "m.py").write_text("def f(a):\n    return a + 1\n", encoding="utf-8")

    def test_progress_goes_to_a_terminal_but_never_into_json_mode(self):
        err = Tty()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            code = mc.main(["--project", str(self.d), "--files", "m.py", "--cmd", self.CHECK])
        self.assertEqual(code, 0)
        self.assertRegex(err.getvalue(), r"\r  testing mutant 4/4 \(\d+s\)   ")
        self.assertTrue(err.getvalue().endswith("\r" + " " * 60 + "\r"))      # the progress line is wiped at the end
        err, out = Tty(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = mc.main(["--project", str(self.d), "--files", "m.py", "--cmd", self.CHECK, "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(err.getvalue(), "")
        self.assertEqual(json.loads(out.getvalue())["killed"], 4)

    def test_on_a_wide_terminal_a_survivor_s_source_is_cut_at_seventy_characters(self):
        long_line = "    return a + 1  # " + "x" * 100
        (self.d / "m.py").write_text("def f(a):\n" + long_line + "\n", encoding="utf-8")
        with mock.patch.dict(os.environ, {"COLUMNS": "200"}):
            code, out, err = run_main("--project", self.d, "--files", "m.py", "--cmd", PASS_CMD, "--min-score", "0")
        self.assertEqual(code, 0, err)
        self.assertNotIn("more (--show 0 lists all)", out)        # all four are shown: nothing is "more"
        rows = [ln for ln in out.splitlines() if ln.startswith("  m.py:2")]
        self.assertEqual(len(rows), 4)
        for ln in rows:
            self.assertEqual(ln.split(" | ", 1)[1], long_line.strip()[:67] + "...")

    def test_the_survivor_list_says_how_many_more_there_are_and_what_to_do_next(self):
        code, out, err = run_main("--project", self.d, "--files", "m.py", "--cmd", PASS_CMD, "--min-score", "0",
                                  "--show", "1")
        self.assertEqual(code, 0, err)
        self.assertIn("\nSURVIVED (4): every test still passed with this bug in place\n", out)
        self.assertEqual(len([ln for ln in out.splitlines() if ln.startswith("  m.py:2")]), 1)
        self.assertIn("\n  ... and 3 more (--show 0 lists all)\n", out)
        self.assertIn("\nNext: for each survivor, write a test that fails when that exact change is applied.\n", out)
        self.assertNotIn("If (and only if)", out)          # the fill-in ignore lines need --print-ignore
        self.assertNotIn("# REASON", out)

    def test_a_clean_run_with_print_ignore_prints_no_ignore_lines(self):
        code, out, err = run_main("--project", self.d, "--files", "m.py", "--cmd", self.CHECK, "--print-ignore")
        self.assertEqual(code, 0, err)
        self.assertIn("0 of 4 mutants survived. Mutation score: 100% (min 80%) -> PASS", out)
        self.assertNotIn("If (and only if)", out)
        self.assertNotIn("Next:", out)

    def test_verbose_marks_only_the_mutants_that_were_killed_by_the_timeout(self):
        hang_on_none = (f'"{sys.executable}" -c "import m, time; r = m.f(1); '
                        f'time.sleep(60 if r is None else 0); assert r == 2"')
        code, out, err = run_main("--project", self.d, "--files", "m.py", "--cmd", hang_on_none, "--timeout", "6",
                                  "--verbose")
        self.assertEqual(code, 0, err)
        rows = [ln for ln in out.splitlines() if ln.startswith("  m.py:2")]
        self.assertEqual(len(rows), 4)
        self.assertEqual([ln.endswith("  [timeout]") for ln in rows],
                         ["`return <value>`" in ln for ln in rows])
        self.assertEqual(sum(ln.endswith("  [timeout]") for ln in rows), 1)

    def test_an_unused_ignore_entry_is_reported_with_its_first_forty_characters(self):
        ignore = self.d / "ignore.txt"
        ignore.write_text("m.py | `+` -> `-` | " + "y" * 50 + "  # because\n", encoding="utf-8")
        code, out, err = run_main("--project", self.d, "--files", "m.py", "--cmd", self.CHECK,
                                  "--ignore-file", ignore)
        self.assertEqual(code, 0, err)
        self.assertIn("\nwarning: unused ignore entry (code changed?): m.py | `+` -> `-` | " + "y" * 40 + "\n", out)

    def test_each_mutant_is_undone_before_the_next_file_is_mutated(self):
        (self.d / "a.py").write_text("def f():\n    return 1\n", encoding="utf-8")
        (self.d / "b.py").write_text("def g():\n    return 5\n", encoding="utf-8")
        only_a = f'"{sys.executable}" -c "import a, b; assert a.f() == 1"'    # nothing checks b.g
        code, out, err = run_main("--project", self.d, "--files", "a.py", "b.py", "--cmd", only_a, "--json",
                                  "--min-score", "0")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.splitlines()[:2], ["{", '  "base": "--files (all lines)",'])   # indented by two
        data = json.loads(out)
        self.assertEqual({k: data[k] for k in ("killed", "survived", "invalid", "equivalent", "accepted", "score")},
                         {"killed": 3, "survived": 3, "invalid": 0, "equivalent": 0, "accepted": 0, "score": 50})
        status = {}
        for m in data["mutants"]:
            status.setdefault(m["file"], set()).add(m["status"])
        self.assertEqual(status, {"a.py": {"killed"}, "b.py": {"survived"}})

    def test_jobs_zero_picks_the_worker_count_itself_and_the_copies_are_trusted(self):
        waits = []

        class Pool(mc.ThreadPoolExecutor):
            def shutdown(self, wait=True, **kw):
                waits.append(wait)
                return super().shutdown(wait=wait, **kw)

        with mock.patch.object(mc.os, "cpu_count", return_value=2), mock.patch.object(mc, "ThreadPoolExecutor", Pool):
            code, out, err = run_main("--project", self.d, "--files", "m.py", "--cmd", self.CHECK, "--jobs", "0")
        self.assertEqual(code, 0, err)
        self.assertEqual(waits, [True, True])     # both pools are joined before the copies are deleted
        self.assertIn("\n  parallel: 2 workers (copies of the project; your files are not touched)\n", out)
        self.assertNotIn("parallel disabled", out)
        self.assertIn("0 of 4 mutants survived. Mutation score: 100%", out)
        self.assertEqual((self.d / "m.py").read_text(encoding="utf-8"), "def f(a):\n    return a + 1\n")

    def test_an_interrupt_kills_the_running_test_processes_instead_of_waiting_for_them(self):
        slow = f'"{sys.executable}" -c "import m, time; time.sleep(0 if m.f(1) == 2 else 40)"'   # only mutants are slow
        raised = []

        def interrupt(futures):
            end = time.time() + 30
            while time.time() < end and len(mc._ACTIVE) < 2:    # both workers have a mutant running
                time.sleep(0.05)
            raised.append(time.time())
            raise KeyboardInterrupt

        with mock.patch.object(mc, "as_completed", interrupt):
            code, out, err = run_main("--project", self.d, "--files", "m.py", "--cmd", slow, "--jobs", "2",
                                      "--timeout", "90")
        waited = time.time() - raised[0]
        self.assertEqual(code, 130)
        self.assertIn("Interrupted. Files restored.", err)
        self.assertLess(waited, 20, "the 40-second test processes were waited for instead of killed")
        self.assertEqual((self.d / "m.py").read_text(encoding="utf-8"), "def f(a):\n    return a + 1\n")
        self.assertEqual(list(Path(tempfile.gettempdir()).glob("honest-tests-work-*")), [])


class FreshSampleTests(unittest.TestCase):
    """Survivors of a second, independent sample (120 mutants that the first sample had not drawn)."""

    def test_an_integer_constant_goes_one_up_and_one_down(self):
        import ast
        sites = mc.py_sites("x = 5\n", None)
        self.assertEqual([(s["label"], s["value"]) for s in sites], [("`5` -> `6`", 6), ("`5` -> `4`", 4)])
        self.assertEqual([mc.py_apply(ast.parse("x = 5\n"), s) for s in sites], ["x = 6\n", "x = 4\n"])

    def test_removing_not_really_removes_it(self):
        import ast
        sites = mc.py_sites("x = not y\n", None)
        self.assertEqual([s["label"] for s in sites], ["remove `not`"])
        self.assertEqual(mc.py_apply(ast.parse("x = not y\n"), sites[0]), "x = y\n")

    def test_the_character_after_a_closing_quote_is_kept(self):
        self.assertEqual(texts("x = 'a'+b"), ["x", "=", "'a'", "+", "b"])

    def test_braces_and_templates_nest_in_both_orders(self):
        self.assertEqual(texts("`${f({a: 1})}` + 2"),      # an object literal inside a substitution
                         ["`${", "f", "(", "{", "a", ":", "1", "}", ")", "}`", "+", "2"])
        self.assertEqual(texts("{ x = `${a}` }"), ["{", "x", "=", "`${", "a", "}`", "}"])   # a template inside a block

    def test_a_type_alias_is_skipped_and_a_statement_of_two_tokens_does_not_crash(self):
        self.assertEqual(js_labels("type N = 1 | 2;\nx = 3\n", ts=True), ["`3` -> `4`", "`3` -> `2`"])
        self.assertEqual(js_labels("x = 1\nfoo;"), ["`1` -> `2`", "`1` -> `0`"])

    def test_a_generic_call_at_the_very_start_of_the_file(self):
        self.assertEqual(js_labels("f<T>(x)\n", ts=True), [])
        self.assertEqual(js_labels("a < b\n", ts=True), ["`<` -> `>=`", "`<` -> `<=`"])

    def test_an_arrow_body_stops_at_the_comma_that_ends_its_argument(self):
        self.assertEqual(mutate("f(x => x + 1, 2)\n", "arrow body -> `undefined`"), "f(x => undefined, 2)\n")

    def test_what_follows_return_decides_whether_there_is_a_value_to_remove(self):
        self.assertEqual(mutate("function f(){ return (a + b); }\n", "`return <value>` -> `return undefined`"),
                         "function f(){ return undefined; }\n")
        self.assertEqual(js_labels("function f(){ return; }\n"), [])
        self.assertEqual(js_labels("function f(){ return }\n"), [])
        self.assertEqual(js_labels("return )"), [])       # broken input: nothing to replace, and no crash


class FreshSampleRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.proj = Path(tempfile.mkdtemp(prefix="ht-heal-"))
        self.addCleanup(shutil.rmtree, self.proj, ignore_errors=True)
        self.bak = Path(tempfile.mkdtemp(prefix=mc.BACKUP_PREFIX))
        self.addCleanup(shutil.rmtree, self.bak, ignore_errors=True)
        self.target = self.proj / "m.py"
        self.stamp = 1_600_000_000 * 10**9
        self.mutant_stamp = self.stamp + 5 * 10**9

    def killed_run(self, off_by_ns, with_backup=True):
        """What a run killed with a mutant on disk leaves behind; the file's mtime is off by `off_by_ns`."""
        if with_backup:
            (self.bak / "1.bak").write_bytes(b"x = 1\n")
        (self.bak / "manifest.json").write_text(json.dumps({"pid": DEAD_PID, "files": {"1.bak": {
            "path": str(self.target), "atime_ns": self.stamp, "mtime_ns": self.stamp,
            "mutant_mtime_ns": self.mutant_stamp}}}), encoding="utf-8")
        self.target.write_bytes(b"x = 2\n")
        os.utime(self.target, ns=(self.mutant_stamp + off_by_ns, self.mutant_stamp + off_by_ns))
        if self.target.stat().st_mtime_ns != self.mutant_stamp + off_by_ns:
            self.skipTest("this filesystem does not keep timestamps to 100 ns")

    def test_a_timestamp_within_a_microsecond_is_still_our_mutant(self):
        self.killed_run(1000)
        self.assertEqual(mc.heal_stale(self.proj), (1, []))
        self.assertEqual(self.target.read_bytes(), b"x = 1\n")
        self.assertEqual(self.target.stat().st_mtime_ns, self.stamp)
        self.assertFalse(self.bak.exists())

    def test_a_timestamp_further_off_means_someone_edited_the_file(self):
        self.killed_run(1100)
        self.assertEqual(mc.heal_stale(self.proj), (0, [self.bak]))
        self.assertEqual(self.target.read_bytes(), b"x = 2\n")
        self.assertTrue((self.bak / "1.bak").exists())

    def test_a_backup_that_cannot_be_read_is_never_thrown_away(self):
        self.killed_run(0, with_backup=False)
        self.assertEqual(mc.heal_stale(self.proj), (0, [self.bak]))
        self.assertEqual(self.target.read_bytes(), b"x = 2\n")
        self.assertTrue((self.bak / "manifest.json").exists())

    def test_guard_write_moves_the_timestamp_forward_and_restore_all_can_run_twice(self):
        self.target.write_bytes(b"x = 1\n")
        os.utime(self.target, ns=(self.stamp, self.stamp))
        g = mc.Guard()
        g.snapshot(self.target)
        g.write(self.target, "x = 2\n", 3)
        st = self.target.stat()                    # before the file is read, which may touch its access time
        self.assertEqual((st.st_atime_ns, st.st_mtime_ns), (self.stamp + 3 * 10**9, self.stamp + 3 * 10**9))
        self.assertEqual(self.target.read_bytes(), b"x = 2\n")
        self.assertIs(g.restore_all(), True)
        self.assertEqual((self.target.read_bytes(), self.target.stat().st_mtime_ns), (b"x = 1\n", self.stamp))
        self.assertIs(g.restore_all(), True)       # the exit hook calls it again: the backup folder is already gone
        self.assertFalse(g.tmp.exists())

    def test_ignore_entries_drop_the_trailing_comment_of_their_own_language(self):
        self.assertEqual(mc.norm_source("    x = a // 2  # halve\n", "py"), "x = a // 2")
        self.assertEqual(mc.norm_source("    x = a / 2;  // halve\n", "js"), "x = a / 2;")
        self.assertEqual(mc.norm_source("  x = h # y;\n", "js"), "x = h # y;")


class FreshSampleCliTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="ht-cli3-")).resolve()
        self.addCleanup(shutil.rmtree, self.d, ignore_errors=True)

    def main(self, *argv):
        return run_main("--project", self.d, *argv)

    def test_an_ignore_line_without_a_reason_is_quoted_up_to_sixty_characters(self):
        line = "m.py | `+` -> `-` | " + "z" * 70
        (self.d / "ig.txt").write_text(line + "\n", encoding="utf-8")
        entries, problems = mc.load_ignores(self.d / "ig.txt")
        self.assertEqual(entries, [])
        self.assertEqual(problems, ["ig.txt:1: needs `file | mutation | source  # reason` (a reason is required): "
                                    + line[:60]])

    def test_thirty_survivors_are_listed_by_default(self):
        (self.d / "m.py").write_text("".join(f"a{i} = 1\n" for i in range(16)), encoding="utf-8")
        code, out, err = self.main("--files", "m.py", "--cmd", PASS_CMD, "--min-score", "0", "--max-mutants", "500")
        self.assertEqual(code, 0, err)
        self.assertIn("\nSURVIVED (32): ", out)
        self.assertEqual(len([ln for ln in out.splitlines() if ln.startswith("  m.py:")]), 30)
        self.assertIn("\n  ... and 2 more (--show 0 lists all)\n", out)

    def test_a_failing_baseline_shows_the_last_twenty_five_lines_of_its_output(self):
        (self.d / "m.py").write_text("x = 1\n", encoding="utf-8")
        failing = f'"{sys.executable}" -c "[print(\'line\', i) for i in range(40)]; raise SystemExit(1)"'
        code, out, err = self.main("--files", "m.py", "--cmd", failing)
        self.assertEqual(code, 2)
        self.assertEqual(err.splitlines()[-25:], [f"line {i}" for i in range(15, 40)])
        self.assertNotIn("line 14\n", err)

    def test_a_file_that_cannot_be_parsed_is_skipped_with_the_first_line_of_the_error(self):
        (self.d / "m.py").write_text("x = 1\n", encoding="utf-8")
        with mock.patch.object(mc, "py_sites", side_effect=ValueError("first line\nsecond line")):
            code, out, err = self.main("--files", "m.py", "--cmd", PASS_CMD)
        self.assertEqual(code, 0, err)
        self.assertIn("\n  skipped : m.py (first line)\n", out)
        self.assertIn("\n  mutants : 0\n", out)

    def test_a_file_with_nothing_to_mutate_is_a_clean_exit(self):
        (self.d / "m.py").write_text('x = "text"\n', encoding="utf-8")
        code, out, err = self.main("--files", "m.py", "--cmd", PASS_CMD)
        self.assertEqual(code, 0, err)
        self.assertIn("\nNo mutable constructs on the changed lines (comments, strings, imports, config).\n", out)
        code, out, err = self.main("--files", "m.py", "--cmd", PASS_CMD, "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {"score": None, "mutants": [], "note": "no mutable constructs"})

    def test_javascript_is_still_mutated_when_node_is_not_installed(self):
        (self.d / "m.js").write_text("module.exports = (a) => a + 1;\n", encoding="utf-8")
        with mock.patch.object(mc.shutil, "which", return_value=None):
            code, out, err = self.main("--files", "m.js", "--cmd", PASS_CMD, "--json", "--min-score", "0")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual((data["survived"], data["invalid"], data["killed"]), (4, 0, 0))

    def test_a_file_gets_its_old_timestamp_back_before_the_next_file_is_mutated(self):
        stamp = 1_600_000_000 * 10**9
        log = self.d / "seen.log"
        for name, body in (("a.py", "def f():\n    return 1\n"), ("b.py", "def g():\n    return 5\n")):
            (self.d / name).write_text(body, encoding="utf-8")
            os.utime(self.d / name, ns=(stamp, stamp))
        record = (f'"{sys.executable}" -c "import os; '
                  f'open({str(log)!r}, \'a\').write(str(os.stat(\'a.py\').st_mtime_ns) + chr(10))"')
        code, out, err = self.main("--files", "a.py", "b.py", "--cmd", record, "--min-score", "0")
        self.assertEqual(code, 0, err)
        seen = [int(x) for x in log.read_text(encoding="utf-8").split()]
        self.assertEqual(len(seen), 7)                          # baseline, three mutants of a.py, three of b.py
        self.assertEqual(seen[0], stamp)
        self.assertTrue(all(x > stamp for x in seen[1:4]), seen)   # a mutant of a.py carries a newer timestamp
        self.assertEqual(seen[4:], [stamp, stamp, stamp])       # while b.py is mutated, a.py is exactly as it was


class TypeScriptHeuristicTests(unittest.TestCase):
    def test_an_arrow_before_a_type_word_is_a_type_only_while_guessing(self):
        # heuristic mode (no parser ranges): `=> void` is taken for a function type and left alone
        self.assertEqual(js_labels("let f: (x: number) => void = g;\n", ts=True), [])
        # with exact ranges from a real parser nothing is guessed: `=> void g()` is an arrow body
        sites = mc.js_sites("const f = () => void g();\n", None, ts=True, exclude=[])
        self.assertEqual([s["label"] for s in sites], ["arrow body -> `undefined`"])
        self.assertEqual(mc.js_apply("const f = () => void g();\n", sites[0]), "const f = () => undefined;\n")


if __name__ == "__main__":
    unittest.main()
