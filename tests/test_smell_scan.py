"""smell_scan.py must flag tests that cannot fail and stay silent on good ones."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _paths

ss = _paths.load(_paths.SMELL_SCAN, "smell_scan")

BAD_PY = '''
from unittest import mock
import unittest


class T(unittest.TestCase):
    def test_truthy(self):
        self.assertTrue(f())

    def test_none(self):
        assert f() is not None

    def test_nothing(self):
        f()

    def test_swallow(self):
        try:
            f()
        except Exception:
            pass

    def test_mock_only(self):
        m = mock.Mock()
        m(1)
        m.assert_called_once()

    def test_patch_unit(self):
        with mock.patch("pkg.f", return_value=1):
            assert f() == 1

    @unittest.skip("later")
    def test_skipped(self):
        assert f() == 2

    def test_tautology(self):
        assert True
'''
GOOD_PY = '''
import unittest
import pytest


class T(unittest.TestCase):
    def test_value(self):
        self.assertEqual(f(2), 4)

    def test_raises(self):
        with self.assertRaises(ValueError):
            f(-1)

    def test_assert_eq(self):
        assert f(3) == 6

    def test_approx(self):
        assert f(0.5) == pytest.approx(1.0)
'''
BAD_JS = """
test('truthy', () => { expect(f()).toBeTruthy(); });
test('nothing', () => { f(); });
test('swallow', () => { try { f(); } catch (e) {} });
test('mock only', () => { const m = jest.fn(); m(1); expect(m).toHaveBeenCalled(); });
it.skip('skipped', () => { expect(f()).toBe(1); });
"""
GOOD_JS = """
test('value', () => { expect(f(2)).toBe(4); });
test('throws', () => { expect(() => f(-1)).toThrow(RangeError); });
"""


def scan(name, text):
    d = Path(tempfile.mkdtemp(prefix="ht-smell-"))
    p = d / name
    p.write_text(text, encoding="utf-8")
    suffix = p.suffix
    fnd, n = (ss.scan_python if suffix == ".py" else ss.scan_js)(str(p), text)
    return {f.rule for f in fnd}, fnd, n


class SmellTests(unittest.TestCase):
    def test_python_bad_tests_are_flagged(self):
        rules, fnd, n = scan("test_bad.py", BAD_PY)
        self.assertEqual(n, 8)
        for r in ("weak-assert", "no-assert", "swallowed-exception", "mock-only", "patches-unit",
                  "skipped", "tautology"):
            self.assertIn(r, rules)

    def test_python_good_tests_are_silent(self):
        rules, fnd, n = scan("test_good.py", GOOD_PY)
        self.assertEqual(n, 4)
        self.assertEqual(rules, set(), [f.msg for f in fnd])

    def test_js_bad_tests_are_flagged(self):
        rules, _, n = scan("bad.test.js", BAD_JS)
        self.assertEqual(n, 4)  # it.skip is reported by the skip rule
        for r in ("weak-assert", "no-assert", "swallowed-exception", "mock-only", "skipped"):
            self.assertIn(r, rules)

    def test_js_good_tests_are_silent(self):
        rules, fnd, _ = scan("good.test.js", GOOD_JS)
        self.assertEqual(rules, set(), [f.msg for f in fnd])

    def test_exit_codes_and_group_filter(self):
        d = Path(tempfile.mkdtemp(prefix="ht-smell-cli-"))
        (d / "test_bad.py").write_text(BAD_PY, encoding="utf-8")
        (d / "test_good.py").write_text(GOOD_PY, encoding="utf-8")
        run = lambda *a: subprocess.run([sys.executable, str(_paths.SMELL_SCAN), *a],
                                        capture_output=True, text=True)
        self.assertEqual(run(str(d / "test_bad.py")).returncode, 1)
        self.assertEqual(run(str(d / "test_good.py")).returncode, 0)
        self.assertEqual(run().returncode, 2)
        out = json.loads(run(str(d / "test_bad.py"), "--group", "mock", "--json").stdout)
        self.assertTrue({f["rule"] for f in out["findings"]} <= {"mock-only", "patches-unit", "mock-heavy"})

    def test_demo_weak_vs_strong(self):
        for demo in ("python-demo", "js-demo"):
            weak = subprocess.run([sys.executable, str(_paths.SMELL_SCAN), str(_paths.ROOT / "examples" / demo / "tests_weak")],
                                  capture_output=True, text=True)
            strong = subprocess.run([sys.executable, str(_paths.SMELL_SCAN), str(_paths.ROOT / "examples" / demo / "tests_strong")],
                                    capture_output=True, text=True)
            self.assertEqual(weak.returncode, 1, demo)
            self.assertEqual(strong.returncode, 0, strong.stdout)

    def test_diff_mode_catches_deleted_skipped_and_loosened(self):
        d = Path(tempfile.mkdtemp(prefix="ht-smell-diff-"))
        g = lambda *a: subprocess.run(["git", *a], cwd=d, check=True, capture_output=True, text=True)
        g("init", "-q"); g("config", "user.email", "t@x.io"); g("config", "user.name", "t")
        (d / "tests").mkdir()
        (d / "tests" / "test_a.py").write_text(
            "def test_one():\n    assert f(1) == 2\n\n\ndef test_two():\n    assert f(2) == 4\n\n\n"
            "def test_three():\n    assert f(3) == 6\n", encoding="utf-8")
        g("add", "-A"); g("commit", "-qm", "base")
        (d / "tests" / "test_a.py").write_text(
            "import pytest\n\n\ndef test_one():\n    assert f(1)\n\n\n@pytest.mark.skip\ndef test_two():\n    assert f(2) == 4\n",
            encoding="utf-8")
        r = subprocess.run([sys.executable, str(_paths.SMELL_SCAN), "--diff", "--json"], cwd=d,
                           capture_output=True, text=True)
        rules = {f["rule"] for f in json.loads(r.stdout)["findings"]}
        self.assertEqual({"deleted-test", "added-skip"} <= rules, True, rules)
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()
