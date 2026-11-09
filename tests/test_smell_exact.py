"""smell_scan.py, rule by rule, with exact expectations (rule, line), not just 'some finding appeared'.

These exist because mutation-checking our own scanner showed the first tests only asserted rule *sets*."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _paths

ss = _paths.load(_paths.SMELL_SCAN, "smell_scan")


def py(src):
    fnd, n = ss.scan_python("t.py", src)
    return sorted((f.rule, f.line) for f in fnd), n


def js(src):
    fnd, n = ss.scan_js("t.js", src)
    return sorted((f.rule, f.line) for f in fnd), n


def one(body, header="def test_a():\n"):
    return py(header + "".join("    " + l + "\n" for l in body.split("\n")))[0]


class PythonAssertionClassification(unittest.TestCase):
    def test_strong_assertions_are_silent(self):
        for body in ("assert f() == 1", "assert f() < 3", "assert f() > 1", "assert f() != 1",
                     "assert f() is None", "assert f() is not 5", "assert a == b == c", "assert len(x) == 2",
                     "assert f() is not None and f() == 1", "assert x in y", "assert not (a == b)"):
            self.assertEqual(one(body), [], body)

    def test_truthy_only_assertions_are_weak(self):
        for body in ("assert x", "assert x.y", "assert x[0]", "assert not x", "assert 1", "assert isinstance(x, int)",
                     "assert callable(x)", "assert any(x)", "assert all(x)", "assert bool(x)",
                     "assert f() is not None", "assert f() > 0", "assert f() >= 0", "assert f() != 0",
                     "assert f() != ''", "assert f() != None", "assert f()", "assert not f()"):
            self.assertEqual(one(body), [("weak-assert", 1)], body)

    def test_one_strong_assertion_makes_the_test_pass(self):
        self.assertEqual(one("assert x\nassert f() == 2"), [])

    def test_tautologies(self):
        for body in ("assert True", "assert x == x", "assert f(1) == f(1)", "self.assertTrue(True)"):
            self.assertEqual(one(body), [("tautology", 1)], body)
        for body in ("assert x == y", "assert False", "self.assertTrue(x)"):
            self.assertNotIn("tautology", [r for r, _ in one(body)], body)

    def test_unittest_methods(self):
        weak = ("self.assertTrue(f())", "self.assertFalse(f())", "self.assertIsNotNone(f())", "self.assertIn(1, f())",
                "self.assertIsInstance(f(), int)", "self.assertGreater(f(), 0)", "self.assertGreaterEqual(f(), 0)",
                "self.assertNotEqual(f(), 0)", "self.assertNotIn(1, f())", "self.assertTrue(f() > 0)")
        for body in weak:
            self.assertEqual(one(body), [("weak-assert", 1)], body)
        strong = ("self.assertEqual(f(), 1)", "self.assertAlmostEqual(f(), 1.0)", "self.assertIsNone(f())",
                  "self.assertIs(f(), x)", "self.assertDictEqual(f(), {})", "self.assertRegex(f(), 'a')",
                  "self.assertTrue(f() == 1)", "self.assertFalse(f() == 1)", "pytest.approx(1.0)",
                  "self.fail('x')", "self.assertCountEqual(f(), [])", "self.assertRaises(ValueError, f)")
        for body in strong:
            self.assertEqual(one(body), [], body)

    def test_context_manager_raises_counts_as_a_check(self):
        self.assertEqual(one("with pytest.raises(ValueError):\n    f()"), [])
        self.assertEqual(one("with self.assertRaises(ValueError):\n    f()"), [])

    def test_no_assertion(self):
        self.assertEqual(one("f()"), [("no-assert", 1)])
        self.assertEqual(py("async def test_a():\n    await f()\n")[0], [("no-assert", 1)])

    def test_only_test_functions_are_scanned(self):
        found, n = py("def helper():\n    f()\n\n\ndef test_a():\n    assert f() == 1\n\n\ndef tester():\n    f()\n")
        self.assertEqual(n, 2)  # `tester` starts with "test": that is the documented convention
        self.assertEqual(found, [("no-assert", 9)])


class PythonMocks(unittest.TestCase):
    def test_mock_only(self):
        self.assertEqual(one("m = mock.Mock()\nm(1)\nm.assert_called_once()"), [("mock-only", 1)])
        self.assertEqual(one("assert m.called"), [("mock-only", 1)])
        self.assertEqual(one("assert m.call_count"), [("mock-only", 1)])
        self.assertEqual(one("m.assert_called_once()\nassert f() == 1"), [])
        for method in ("assert_called", "assert_any_call", "assert_has_calls", "assert_not_called", "assert_awaited"):
            self.assertEqual(one(f"m.{method}()"), [("mock-only", 1)], method)

    def test_patching_the_function_under_test(self):
        self.assertEqual(one('with mock.patch("pkg.f"):\n    assert f() == 1'), [("patches-unit", 1)])
        self.assertEqual(one('with patch.object(pkg, "f"):\n    assert f() == 1'), [("patches-unit", 1)])
        self.assertEqual(one('with mock.patch("pkg.g"):\n    assert f() == 1'), [])
        self.assertEqual(one('with mock.patch("pkg.f"):\n    assert 1 == 1 + 0'), [])
        deco = '@mock.patch("pkg.f")\ndef test_a(m):\n    assert f() == 1\n'
        self.assertEqual(py(deco)[0], [("patches-unit", 2)])
        self.assertEqual(py('@mock.patch("pkg.test_a")\ndef test_a(m):\n    assert test_a() == 1\n')[0], [])

    def test_mock_heavy_starts_at_three(self):
        two = "a = Mock()\nb = MagicMock()\nassert f(a, b) == 1"
        three = "a = Mock()\nb = MagicMock()\nc = AsyncMock()\nassert f(a, b, c) == 1"
        self.assertEqual(one(two), [])
        self.assertEqual(one(three), [("mock-heavy", 1)])
        found, _ = py("@patch('a.b')\n@patch('a.c')\n@patch('a.d')\ndef test_a(x, y, z):\n    assert f() == 1\n")
        self.assertEqual(found, [("mock-heavy", 4)])


class PythonSwallowSkipSnapshot(unittest.TestCase):
    def test_swallowed_exception_is_reported_at_the_handler(self):
        src = "def test_a():\n    try:\n        f()\n    except Exception:\n        pass\n"
        self.assertEqual(py(src)[0], [("swallowed-exception", 4)])  # and not also no-assert
        for handler in ("raise", "assert False", "self.fail('no')", "self.assertEqual(1, 2)"):
            src = f"def test_a():\n    try:\n        f()\n    except Exception:\n        {handler}\n    assert f() == 1\n"
            self.assertEqual(py(src)[0], [], handler)
        src = ("def test_a():\n    try:\n        f()\n    except A:\n        pass\n    except B:\n        pass\n")
        self.assertEqual(py(src)[0], [("swallowed-exception", 4), ("swallowed-exception", 6)])

    def test_skipped_decorators_and_calls(self):
        for deco in ("@unittest.skip('x')", "@unittest.skipIf(True, 'x')", "@unittest.skipUnless(False, 'x')",
                     "@pytest.mark.skip", "@pytest.mark.skipif(True, reason='x')", "@pytest.mark.xfail", "@skip"):
            self.assertEqual(py(f"{deco}\ndef test_a():\n    assert f() == 1\n")[0], [("skipped", 2)], deco)
        self.assertEqual(one("self.skipTest('x')"), [("skipped", 2)])
        self.assertEqual(one("pytest.skip('x')"), [("skipped", 2)])
        self.assertEqual(one("pytest.xfail('x')"), [("skipped", 2)])
        self.assertEqual(one("assert f() == 1"), [])
        self.assertEqual(py("@pytest.mark.parametrize('a', [1])\ndef test_a(a):\n    assert f(a) == 1\n")[0], [])

    def test_snapshot(self):
        self.assertEqual(one("assert_match_snapshot(f())\nassert f() == 1"), [("snapshot", 2)])
        self.assertEqual(one("snapshot.assert_match(f())"), [("snapshot", 2)])
        self.assertEqual(one("re.match('a', 'b')\nassert f() == 1"), [])

    def test_finding_dict_has_severity_and_fix(self):
        f = ss.Finding("a.py", 3, "no-assert", "m", "t").as_dict()
        self.assertEqual((f["severity"], f["line"], f["test"], f["fix"]),
                         ("BAD", 3, "t", ss.ADVICE["no-assert"]))
        self.assertEqual(ss.Finding("a.py", 3, "snapshot", "m").as_dict()["severity"], "SUSPECT")


class JavaScript(unittest.TestCase):
    def test_exact_lines_and_rules(self):
        src = ("test('ok', () => { expect(f()).toBe(1); });\n"
               "test('truthy', () => { expect(f()).toBeTruthy(); });\n"
               "it('nothing', () => { f(); });\n"
               "test('mock', () => { const m = jest.fn(); m(); expect(m).toHaveBeenCalled(); });\n"
               "test('tauto', () => { expect(true).toBe(true); });\n"
               "test('swallow', () => {\n  try { f(); }\n  catch (e) { log(e); }\n});\n")
        found, n = js(src)
        self.assertEqual(n, 6)
        self.assertEqual(found, [("mock-only", 4), ("no-assert", 3), ("swallowed-exception", 8),
                                 ("tautology", 5), ("weak-assert", 2)])

    def test_strong_and_typeof_only(self):
        self.assertEqual(js("test('a', () => { expect(f()).toEqual({a: 1}); });")[0], [])
        self.assertEqual(js("test('a', () => { assert.strictEqual(f(), 1); });")[0], [])
        self.assertEqual(js("test('a', () => { expect(typeof f()).toBe('number'); });")[0], [("weak-assert", 1)])
        self.assertEqual(js("test('a', () => { expect(f()).toBeDefined(); });")[0], [("weak-assert", 1)])
        self.assertEqual(js("test('a', () => { expect(f()).not.toBeNull(); });")[0], [("weak-assert", 1)])
        self.assertEqual(js("test('a', () => { expect(f()).toBeGreaterThan(0); });")[0], [("weak-assert", 1)])
        self.assertEqual(js("test('a', () => { assert.ok(f()); });")[0], [("weak-assert", 1)])

    def test_catch_that_asserts_or_rethrows_is_fine(self):
        for handler in ("throw e;", "expect(e.message).toBe('x');", "assert.fail('x');", "done(e);"):
            self.assertEqual(js("test('a', () => { try { expect(f()).toBe(1); } catch (e) { %s } });" % handler)[0], [], handler)

    def test_skip_snapshot_and_mock_rules(self):
        for skip in ("xit('a', () => {});", "xtest('a', () => {});", "xdescribe('a', () => {});",
                     "it.skip('a', () => {});", "test.todo('a');", "describe.only('a', () => {});"):
            self.assertEqual([r for r, _ in js("\n" + skip + "\n")[0]].count("skipped"), 1, skip)
        self.assertEqual(js("test('a', () => { expect(f()).toMatchSnapshot(); });")[0],
                         [("snapshot", 1), ("weak-assert", 1)])
        two = "test('a', () => { const a = jest.fn(); const b = vi.fn(); expect(f(a, b)).toBe(1); });"
        three = "test('a', () => { const a = jest.fn(); const b = vi.fn(); const c = sinon.stub(); expect(f(a, b, c)).toBe(1); });"
        self.assertEqual(js(two)[0], [])
        self.assertEqual(js(three)[0], [("mock-heavy", 1)])
        spy = "test('a', () => { jest.spyOn(obj, 'run'); expect(obj.run(1)).toBe(2); });"
        self.assertEqual(js(spy)[0], [("patches-unit", 1)])
        self.assertEqual(js("test('a', () => { jest.spyOn(obj, 'run'); expect(other(1)).toBe(2); });")[0], [])

    def test_names_with_quotes_and_each(self):
        self.assertEqual(js("it.each([1,2])('does \\'x\\'', (n) => { f(n); });")[0], [("no-assert", 1)])
        self.assertEqual(js("test(`tpl`, () => { f(); });")[0], [("no-assert", 1)])


class Paths(unittest.TestCase):
    def test_is_test_path(self):
        for p in ("test_a.py", "a_test.py", "conftest.py", "a.test.ts", "a.spec.jsx", "x/tests/a.py", "x/__tests__/a.js",
                  "tests_weak/a.py", "src/test/a.py", "C:\\x\\tests\\a.py", "a.test.mjs", "A.TEST.JS"):
            self.assertTrue(ss.is_test_path(p), p)
        for p in ("a.py", "src/contest.py", "latest.js", "x/y/a.test.py", "attests/a.py", "a.spec.txt", "testing.py"):
            self.assertFalse(ss.is_test_path(p), p)


class Driver(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="ht-drv-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def run_cli(self, *a, cwd=None):
        return subprocess.run([sys.executable, str(_paths.SMELL_SCAN), *a], capture_output=True, text=True,
                              cwd=cwd or self.d)

    def write(self, name, text):
        p = self.d / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def test_score_and_counts(self):
        good = "def test_%d():\n    assert f(%d) == %d\n\n\n"
        src = "".join(good % (i, i, i) for i in range(9)) + "def test_bad():\n    f()\n"
        self.write("test_a.py", src)
        out = json.loads(self.run_cli("test_a.py", "--json").stdout)
        self.assertEqual((out["tests_scanned"], out["tests_flagged"], out["scan_score"]), (10, 1, 9))
        self.write("test_b.py", "def test_x():\n    f()\n\n\ndef test_y():\n    f()\n\n\ndef test_z():\n    assert f() == 1\n\n\n"
                                "def test_w():\n    assert f() == 1\n")
        out = json.loads(self.run_cli("test_b.py", "--json").stdout)
        self.assertEqual((out["tests_scanned"], out["tests_flagged"], out["scan_score"]), (4, 2, 5))
        self.write("test_c.py", "def helper():\n    pass\n")
        out = json.loads(self.run_cli("test_c.py", "--json").stdout)
        self.assertEqual((out["tests_scanned"], out["scan_score"]), (0, 10))

    def test_text_report_format_and_exit_codes(self):
        self.write("test_a.py", "def test_bad():\n    f()\n\n\n@pytest.mark.skip\ndef test_s():\n    assert f() == 1\n")
        r = self.run_cli("test_a.py")
        self.assertEqual(r.returncode, 1)
        self.assertIn("BAD (2)", r.stdout)
        self.assertIn("test_a.py:1  [no-assert]  `test_bad` has no assertion", r.stdout)
        self.assertIn("test_a.py:6  [skipped]  `test_s` is skipped/xfail", r.stdout)
        self.assertIn("fix: " + ss.ADVICE["no-assert"], r.stdout)
        self.assertIn("2 tests scanned, 2 flagged as unable to fail. Scan score: 0/10", r.stdout)
        self.write("test_snap.py", "def test_s():\n    snapshot.assert_match(f())\n    assert f() == 1\n")
        r = self.run_cli("test_snap.py")
        self.assertEqual(r.returncode, 0)  # SUSPECT only
        self.assertIn("SUSPECT (1)", r.stdout)
        self.assertNotIn("BAD", r.stdout)

    def test_usage_errors_exit_two(self):
        self.assertEqual(self.run_cli().returncode, 2)
        r = self.run_cli("nope.py")
        self.assertEqual(r.returncode, 2)
        self.assertIn("nope.py does not exist", r.stderr)

    def test_group_filter(self):
        self.write("test_a.py", "def test_a():\n    f()\n\n\ndef test_b():\n    m.assert_called()\n")
        rules = lambda g: {f["rule"] for f in json.loads(self.run_cli("test_a.py", "--group", g, "--json").stdout)["findings"]}
        self.assertEqual(rules("mock"), {"mock-only"})
        self.assertEqual(rules("assertion"), {"no-assert"})
        self.assertEqual(rules("all"), {"mock-only", "no-assert"})

    def test_directory_walk_skips_non_tests_and_vendored_dirs(self):
        bad = "def test_a():\n    f()\n"
        self.write("tests/test_a.py", bad)
        self.write("tests/sub/c.test.js", "test('a', () => { f(); });")
        self.write("tests/data.txt", "x")
        self.write("lib/util.py", bad)                      # not under a tests dir, not named test_*
        self.write("lib/test_util.py", bad)                 # named like a test: scanned
        self.write("node_modules/pkg/test_b.py", bad)       # vendored: skipped
        self.write(".venv/test_c.py", bad)
        out = json.loads(self.run_cli("tests", "lib", "node_modules", ".venv", "--json").stdout)
        self.assertEqual(sorted({f["file"] for f in out["findings"]}),
                         ["lib/test_util.py", "tests/sub/c.test.js", "tests/test_a.py"])

    def test_unscannable_files_are_reported_not_hidden(self):
        self.write("test_broken.py", "def test_a(:\n")
        r = self.run_cli("test_broken.py")
        self.assertIn("NOT SCANNED: test_broken.py (SyntaxError)", r.stdout)
        out = json.loads(self.run_cli("test_broken.py", "--json").stdout)
        self.assertEqual(out["not_scanned"], ["test_broken.py (SyntaxError)"])

    def test_ignore_comment_on_the_line_or_the_line_above_only(self):
        body = "def test_{n}():\n    f()\n\n\n"
        self.write("test_a.py",
                   "# honest-tests: ignore\n" + body.format(n="above") +
                   body.format(n="same").replace("def test_same():", "def test_same():  # honest-tests: ignore") +
                   "# honest-tests: ignore\n\n" + body.format(n="two_above") +
                   body.format(n="plain"))
        names = {f["test"] for f in json.loads(self.run_cli("test_a.py", "--json").stdout)["findings"]}
        self.assertEqual(names, {"test_two_above", "test_plain"})

    def test_ignore_for_a_file_that_vanished_does_not_crash(self):
        f = ss.Finding(str(self.d / "gone.py"), 5, "no-assert", "m", "t")
        self.assertFalse(ss._suppressed(f))
        self.write("here.py", "x\n")
        self.assertFalse(ss._suppressed(ss.Finding(str(self.d / "here.py"), 1, "no-assert", "m", "t")))
        self.assertFalse(ss._suppressed(ss.Finding(str(self.d / "here.py"), 99, "no-assert", "m", "t")))


class DiffMode(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="ht-dm-"))
        self.g("init", "-q")
        self.g("config", "user.email", "t@x.io")
        self.g("config", "user.name", "t")
        (self.d / "tests").mkdir()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def g(self, *a):
        subprocess.run(["git", *a], cwd=self.d, check=True, capture_output=True, text=True)

    def commit(self, text, name="tests/test_a.py"):
        (self.d / name).write_text(text, encoding="utf-8")
        self.g("add", "-A")
        self.g("commit", "-qm", "c")

    def diff(self, new, name="tests/test_a.py"):
        (self.d / name).write_text(new, encoding="utf-8")
        r = subprocess.run([sys.executable, str(_paths.SMELL_SCAN), "--diff", "--json"], cwd=self.d,
                           capture_output=True, text=True)
        return json.loads(r.stdout), r.returncode

    BASE = ("def test_one():\n    assert f(1) == 2\n\n\ndef test_two():\n    assert f(2) == 4\n\n\n"
            "def test_three():\n    assert f(3) == 6\n")

    def test_deleted_tests_exact(self):
        self.commit(self.BASE)
        out, rc = self.diff("def test_one():\n    assert f(1) == 2\n")
        f = [x for x in out["findings"] if x["rule"] == "deleted-test"]
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["file"], "tests/test_a.py")
        self.assertIn("2 test definition(s) removed (2 removed, 0 added)", f[0]["message"])
        self.assertEqual((rc, out["scan_score"]), (1, 6))

    def test_replacing_a_test_with_another_is_not_a_deletion(self):
        self.commit(self.BASE)
        out, _ = self.diff(self.BASE.replace("test_three", "test_four"))
        self.assertEqual([x["rule"] for x in out["findings"]], [])

    def test_added_skip_reports_the_exact_line(self):
        self.commit(self.BASE)
        out, _ = self.diff("import pytest\n\n\n" + self.BASE.replace(
            "def test_two", "@pytest.mark.skip\ndef test_two"))
        sk = [x for x in out["findings"] if x["rule"] == "added-skip"]
        self.assertEqual([x["line"] for x in sk], [8])
        self.assertIn("@pytest.mark.skip", sk[0]["message"])

    def test_loosened_assertion_counts_and_line(self):
        self.commit(self.BASE)
        out, rc = self.diff(self.BASE.replace("assert f(1) == 2", "assert f(1)").replace("assert f(2) == 4", "assert f(2) is not None"))
        lo = [x for x in out["findings"] if x["rule"] == "loosened"]
        self.assertEqual([x["line"] for x in lo], [2, 6])  # one finding per hunk
        self.assertEqual(len(lo), 2)
        self.assertEqual(lo[0]["line"], 2)
        self.assertIn("strong assertions 1 -> 0, weak assertions +1", lo[0]["message"])
        self.assertEqual(rc, 1)

    def test_loosened_is_not_reported_for_equal_strength_edits(self):
        self.commit(self.BASE)
        out, rc = self.diff(self.BASE.replace("f(1) == 2", "f(1) == 3"))
        self.assertEqual(out["findings"], [])
        self.assertEqual(rc, 0)

    def test_non_test_files_are_ignored(self):
        self.commit("x = 1\n", "lib.py")
        out, _ = self.diff("x = 2\n", "lib.py")
        self.assertEqual(out["findings"], [])

    def test_diff_against_a_ref_and_bad_ref(self):
        self.commit(self.BASE)
        (self.d / "tests" / "test_a.py").write_text("def test_one():\n    assert f(1) == 2\n", encoding="utf-8")
        ok = subprocess.run([sys.executable, str(_paths.SMELL_SCAN), "--diff", "HEAD", "--json"], cwd=self.d,
                            capture_output=True, text=True)
        self.assertEqual(ok.returncode, 1)
        bad = subprocess.run([sys.executable, str(_paths.SMELL_SCAN), "--diff", "no-such-ref"], cwd=self.d,
                             capture_output=True, text=True)
        self.assertEqual(bad.returncode, 2)
        self.assertIn("git diff failed", bad.stderr)


if __name__ == "__main__":
    unittest.main()
