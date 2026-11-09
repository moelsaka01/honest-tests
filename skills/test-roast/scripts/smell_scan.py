#!/usr/bin/env python3
"""Static scan for tests that cannot fail: no assertions, truthy-only checks, mock-only checks,
swallowed exceptions, skips, snapshots, patched units under test, and (with --diff) tests deleted,
skipped or loosened in the same change.

    python smell_scan.py tests/                      # files or directories (Python and JS/TS)
    python smell_scan.py --diff                      # test changes vs HEAD (or --diff origin/main)
    python smell_scan.py tests/ --group mock         # only mock findings (mock-audit)
    python smell_scan.py tests/ --group assertion    # only assertion findings (assertion-strength)

This is a smell detector, not a proof: use mutation_check.py to prove what the tests really catch.
A reviewed false positive can be silenced with a `honest-tests: ignore` comment on or above the line.
Python: `ast`. JavaScript/TypeScript: a brace-matching regex scanner. Standard library only.
Exit code 1 if any BAD finding, 0 otherwise, 2 on usage errors.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
from pathlib import Path

GROUPS = {
    "mock": {"mock-only", "patches-unit", "mock-heavy"},
    "assertion": {"no-assert", "weak-assert", "tautology", "swallowed-exception", "skipped",
                  "snapshot", "loosened", "deleted-test", "added-skip"},
}
BAD = {"no-assert", "weak-assert", "mock-only", "swallowed-exception", "tautology",
       "deleted-test", "added-skip", "loosened", "skipped"}
ADVICE = {
    "no-assert": "assert on the return value or the side effect",
    "weak-assert": "assert the exact expected value, not just that something came back",
    "mock-only": "assert on observable results; mock calls alone only prove wiring",
    "swallowed-exception": "remove the try/except, or use assertRaises / pytest.raises / toThrow",
    "tautology": "assert something the code under test produced",
    "skipped": "fix the test or delete it with a reason; do not park it",
    "snapshot": "review the snapshot and add one explicit assertion beside it",
    "patches-unit": "patch the dependency, not the function you are testing",
    "mock-heavy": "use a real object or a small fake; keep one real code path under test",
    "deleted-test": "restore the test or explain why the behaviour is gone",
    "added-skip": "do not skip or xfail to get green; fix the code or the test",
    "loosened": "restore the exact assertion; a looser one hides the bug it used to catch",
}
SKIP_DIR_NAMES = {"node_modules", ".venv", "venv", ".git", "__pycache__", "dist", "build"}


def is_test_path(p: str) -> bool:
    p = p.replace("\\", "/").lower()
    name = p.rsplit("/", 1)[-1]
    return (name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py"
            or bool(re.search(r"\.(test|spec)\.[cm]?[jt]sx?$", name))
            or any(d in ("tests", "test", "__tests__") or d.startswith("tests_")
                   for d in p.split("/")[:-1]))


class Finding:
    def __init__(self, path, line, rule, msg, test=""):
        self.path, self.line, self.rule, self.msg, self.test = path, line, rule, msg, test

    def as_dict(self):
        return {"file": self.path, "line": self.line, "rule": self.rule, "test": self.test,
                "message": self.msg, "fix": ADVICE[self.rule],
                "severity": "BAD" if self.rule in BAD else "SUSPECT"}


# --------------------------------------------------------------------------- python

def _name(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _weak_expr(e) -> bool:
    if isinstance(e, ast.Constant):
        return True
    if isinstance(e, (ast.Name, ast.Attribute, ast.Subscript)):
        return True
    if isinstance(e, ast.Call):
        return True  # `assert f(x)` only says "truthy", the same as assertTrue(f(x))
    if isinstance(e, ast.UnaryOp) and isinstance(e.op, ast.Not):
        return _weak_expr(e.operand)
    if isinstance(e, ast.Compare) and len(e.ops) == 1:
        op, right = e.ops[0], e.comparators[0]
        if isinstance(op, ast.IsNot) and isinstance(right, ast.Constant) and right.value is None:
            return True
        if isinstance(op, (ast.Gt, ast.GtE, ast.NotEq)) and isinstance(right, ast.Constant) \
                and right.value in (0, "", None):
            return True
        if isinstance(op, ast.Gt) and isinstance(right, ast.Constant) and right.value == 0:
            return True
    return False


def _tautology(e) -> bool:
    if isinstance(e, ast.Constant) and e.value is True:
        return True
    return isinstance(e, ast.Compare) and len(e.ops) == 1 and isinstance(e.ops[0], ast.Eq) \
        and ast.dump(e.left) == ast.dump(e.comparators[0])


WEAK_METHODS = {"assertTrue", "assertFalse", "assertIsNotNone", "assertIsInstance", "assertIn",
                "assertGreater", "assertGreaterEqual", "assertNotEqual", "assertNotIn"}
STRONG_METHODS_PREFIX = ("assertEqual", "assertAlmost", "assertRaises", "assertCountEqual",
                         "assertListEqual", "assertDictEqual", "assertSequenceEqual",
                         "assertIs", "assertIsNone", "assertTupleEqual", "assertSetEqual",
                         "assertMultiLineEqual", "assertRegex", "assertWarns", "assertLogs")


def scan_python(path: str, src: str):
    out, n_tests = [], 0
    tree = ast.parse(src)
    for fn in ast.walk(tree):
        if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or not fn.name.startswith("test"):
            continue
        n_tests += 1
        strong = weak = mock_checks = 0
        tauto = False
        patch_targets, called, mocks = [], set(), 0
        deco_names = [_name(d.func if isinstance(d, ast.Call) else d) for d in fn.decorator_list]
        for d in fn.decorator_list:
            dn = _name(d.func if isinstance(d, ast.Call) else d)
            if dn.endswith("patch") or ".patch." in dn + ".":
                mocks += 1
                if isinstance(d, ast.Call) and d.args and isinstance(d.args[0], ast.Constant):
                    patch_targets.append(str(d.args[0].value))
        if any(re.search(r"(^|\.)(skip|skipif|skipunless|xfail)$", n.lower()) for n in deco_names):
            out.append(Finding(path, fn.lineno, "skipped", f"`{fn.name}` is skipped/xfail", fn.name))
        in_decorators = {id(x) for d in fn.decorator_list for x in ast.walk(d)}
        snaps = 0
        for node in ast.walk(fn):
            if id(node) in in_decorators:
                continue
            if isinstance(node, ast.Assert):
                if _tautology(node.test):
                    tauto = True
                elif _weak_expr(node.test):
                    weak += 1
                else:
                    strong += 1
            elif isinstance(node, ast.Call):
                n = _name(node.func)
                last = n.rsplit(".", 1)[-1]
                called.add(last)
                if last in WEAK_METHODS:
                    tauto_arg = node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value is True
                    if tauto_arg:
                        tauto = True
                    elif last in ("assertTrue", "assertFalse") and node.args and \
                            isinstance(node.args[0], ast.Compare) and not _weak_expr(node.args[0]):
                        strong += 1
                    else:
                        weak += 1
                elif last.startswith(STRONG_METHODS_PREFIX) or n in ("pytest.raises", "pytest.approx",
                                                                       "pytest.fail", "self.fail"):
                    strong += 1
                elif re.match(r"assert_(called|any_call|has_calls|not_called|awaited)", last):
                    mock_checks += 1
                elif last in ("skipTest",) or n in ("pytest.skip", "pytest.xfail"):
                    out.append(Finding(path, node.lineno, "skipped", f"`{fn.name}` calls {n}()", fn.name))
                elif last in ("patch", "object", "patch_object", "Mock", "MagicMock", "AsyncMock",
                              "create_autospec", "spy"):
                    if last in ("patch", "object") and not (n.endswith("patch") or n.endswith("patch.object")):
                        continue
                    mocks += 1
                    if last == "patch" and node.args and isinstance(node.args[0], ast.Constant):
                        patch_targets.append(str(node.args[0].value))
                    if n.endswith("patch.object") and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
                        patch_targets.append(str(node.args[1].value))
                elif "snapshot" in n.lower() or last in ("match", "matches_snapshot"):
                    if "snapshot" in n.lower():
                        snaps += 1
                        out.append(Finding(path, node.lineno, "snapshot", "snapshot assertion", fn.name))
            elif isinstance(node, ast.Attribute) and node.attr in ("called", "call_count", "call_args",
                                                                    "call_args_list", "assert_called"):
                mock_checks += 1
            elif isinstance(node, ast.Try):
                for h in node.handlers:
                    body_has_check = any(isinstance(x, (ast.Raise, ast.Assert)) or
                                         (isinstance(x, ast.Expr) and isinstance(x.value, ast.Call) and
                                          _name(x.value.func).rsplit(".", 1)[-1].startswith(("assert", "fail")))
                                         for x in ast.walk(h))
                    if not body_has_check:
                        out.append(Finding(path, h.lineno, "swallowed-exception",
                                           f"`{fn.name}` catches an exception and asserts nothing", fn.name))
        total_checks = strong + weak + mock_checks + snaps
        if tauto:
            out.append(Finding(path, fn.lineno, "tautology", f"`{fn.name}` asserts something always true", fn.name))
        if total_checks == 0 and not tauto and not any(f.rule == "swallowed-exception" and f.test == fn.name for f in out) \
                and not any(f.rule == "skipped" and f.test == fn.name for f in out):
            out.append(Finding(path, fn.lineno, "no-assert", f"`{fn.name}` has no assertion", fn.name))
        elif strong == 0 and mock_checks > 0:
            out.append(Finding(path, fn.lineno, "mock-only",
                               f"`{fn.name}` only checks that mocks were called", fn.name))
        elif strong == 0 and weak > 0:
            out.append(Finding(path, fn.lineno, "weak-assert",
                               f"`{fn.name}`: every assertion is truthy/None/type-only ({weak})", fn.name))
        for t in patch_targets:
            leaf = t.rsplit(".", 1)[-1]
            if leaf in called and leaf != fn.name:
                out.append(Finding(path, fn.lineno, "patches-unit",
                                   f"`{fn.name}` patches `{t}` and also calls `{leaf}()`", fn.name))
        if mocks >= 3:
            out.append(Finding(path, fn.lineno, "mock-heavy",
                               f"`{fn.name}` builds {mocks} mocks/patches", fn.name))
    return out, n_tests


# --------------------------------------------------------------------------- javascript

JS_TEST = re.compile(r"\b(it|test)(\.each\([^)]*\))?\s*\(\s*(['\"`])((?:\\.|(?!\3).)*)\3")
JS_WEAK = re.compile(r"\.(toBeTruthy|toBeFalsy|toBeDefined|toBeUndefined|toBeNull|toBeInstanceOf|"
                     r"toHaveBeenCalled(?:Times|With)?|toBeGreaterThan\(\s*0\s*\))\b|"
                     r"\.not\.(toBeNull|toBeUndefined|toBe\(undefined\))|"
                     r"\bassert(?:\.ok|\.notStrictEqual\([^,]+,\s*undefined)|\bassert\(\s*[\w.]+\(?[^=]*\)?\s*\)")
JS_STRONG = re.compile(r"\.(toBe|toEqual|toStrictEqual|toMatchObject|toContain|toThrow|toThrowError|"
                       r"toHaveLength|toBeCloseTo|toHaveProperty|rejects|resolves)\(|"
                       r"\bassert\.(strictEqual|deepStrictEqual|equal|deepEqual|throws|rejects|match)\(|"
                       r"\bexpect\(.*\)\.(to\.)?(equal|eql|deep)")
JS_MOCKCHK = re.compile(r"toHaveBeenCalled|toBeCalled|\.mock\.calls|\.mock\.callCount|callCount\(|"
                        r"sinon\.assert|\.calledWith|\.calledOnce")
JS_SKIP = re.compile(r"\b(xit|xtest|xdescribe)\s*\(|\b(it|test|describe)\.(skip|todo|only)\b")
JS_SNAP = re.compile(r"toMatch(Inline)?Snapshot|toThrowErrorMatchingSnapshot")


def _brace_block(src: str, start: int):
    i = src.find("{", start)
    if i < 0:
        return None
    depth, j = 0, i
    while j < len(src):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
        j += 1
    return None


def scan_js(path: str, src: str):
    out, n_tests = [], 0
    for m in JS_TEST.finditer(src):
        name = m.group(4)
        line = src.count("\n", 0, m.start()) + 1
        block = _brace_block(src, m.end())
        if block is None:
            continue
        n_tests += 1
        strong = len(JS_STRONG.findall(block))
        mockchk = len(JS_MOCKCHK.findall(block))
        weak = max(len(JS_WEAK.findall(block)) - mockchk, 0)
        typeof_only = len(re.findall(r"(?:assert\.\w+|expect)\(\s*typeof\b", block))
        strong, weak = max(strong - typeof_only, 0), weak + typeof_only
        has_expect = bool(re.search(r"\bexpect\(|\bassert\b|\.should\b|\.expect\b", block))
        for cm in re.finditer(r"\bcatch\b[^{]*\{", block):
            body = _brace_block(block, cm.start())
            if body is not None and not re.search(r"\bthrow\b|\bexpect\(|\bassert|\.fail\(|\bdone\(", body):
                out.append(Finding(path, line + block.count("\n", 0, cm.start()), "swallowed-exception",
                                   f"`{name}` catches an error and asserts nothing", name))
        if re.search(r"expect\((true|1)\)\.toBe\(\1\)|assert\.ok\(true\)|assert\(true\)", block):
            out.append(Finding(path, line, "tautology", f"`{name}` asserts something always true", name))
        elif not has_expect and not any(f.test == name and f.rule == "swallowed-exception" for f in out):
            out.append(Finding(path, line, "no-assert", f"`{name}` has no assertion", name))
        elif strong == 0 and mockchk == 0 and has_expect:
            out.append(Finding(path, line, "weak-assert",
                               f"`{name}`: every assertion is truthy/defined/type-only", name))
        elif strong == 0 and mockchk > 0:
            out.append(Finding(path, line, "mock-only", f"`{name}` only checks that mocks were called", name))
        if JS_SNAP.search(block):
            out.append(Finding(path, line, "snapshot", f"`{name}` relies on a snapshot", name))
        for pm in re.finditer(r"(?:mock\.method|spyOn)\(\s*[\w.]+\s*,\s*['\"](\w+)['\"]", block):
            if re.search(r"\.%s\(" % re.escape(pm.group(1)), block[pm.end():]):
                out.append(Finding(path, line, "patches-unit",
                                   f"`{name}` stubs `{pm.group(1)}` and then calls it", name))
        n_mocks = len(re.findall(r"\b(?:jest|vi)\.(?:fn|mock|spyOn)\(|\bsinon\.(?:stub|spy|mock)\(|"
                                 r"\bt\.mock\.(?:fn|method)\(", block))
        if n_mocks >= 3:
            out.append(Finding(path, line, "mock-heavy", f"`{name}` builds {n_mocks} mocks/spies", name))
    for sm in JS_SKIP.finditer(src):
        out.append(Finding(path, src.count("\n", 0, sm.start()) + 1, "skipped",
                           f"skipped/focused/todo test: `{sm.group(0).strip()}`"))
    return out, n_tests


# --------------------------------------------------------------------------- diff mode

DEF_RE = re.compile(r"^\s*(?:async\s+)?def\s+test\w*\(|^\s*(?:it|test)(?:\.each\([^)]*\))?\s*\(")
SKIP_RE = re.compile(r"@(?:pytest\.mark\.)?(?:skip|skipif|xfail)\b|pytest\.(?:skip|xfail)\(|"
                     r"\bskipTest\(|\bunittest\.skip|\b(?:xit|xtest|xdescribe)\s*\(|"
                     r"\b(?:it|test|describe)\.(?:skip|todo)\b")
STRONG_LINE = re.compile(r"assertEqual|assertAlmostEqual|assertRaises|assertDictEqual|assertListEqual|"
                         r"assertIs\(|assertIsNone|pytest\.raises|pytest\.approx|\bassert\b.*(==|!=|<=|>=)|"
                         r"\.toBe\(|\.toEqual\(|\.toStrictEqual\(|\.toThrow|\.toHaveLength|\.toContain\(|"
                         r"assert\.(strictEqual|deepStrictEqual|equal|deepEqual|throws)\(")
WEAK_LINE = re.compile(r"assertTrue|assertIsNotNone|assertIsInstance|assertGreater|\bis not None\b|"
                       r"\bassert\s+[\w.()\[\]]+\s*$|toBeTruthy|toBeDefined|\.not\.toBeNull|assert\.ok\(|"
                       r"toBeGreaterThan\(0\)|\bpass\b|toHaveBeenCalled")


def scan_diff(base: str):
    r = subprocess.run(["git", "-c", "core.quotepath=off", "diff", "--no-renames", "--unified=0",
                        "--no-color", "--no-ext-diff", base, "--"], capture_output=True, text=True,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        raise SystemExit(f"error: git diff failed: {r.stderr.strip()}")
    out, path, hunks = [], None, {}
    cur = None
    for line in r.stdout.splitlines():
        if line.startswith("+++ "):
            p = line[4:].strip("\t")
            path = None if p == "/dev/null" else (p[2:] if p.startswith("b/") else p)
            cur = None
        elif line.startswith("--- ") and path is None:
            pass
        elif line.startswith("@@"):
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)", line)
            cur = {"line": int(m.group(1)) if m else 1, "rm": [], "add": []}
            hunks.setdefault(path, []).append(cur)
        elif cur is not None and line.startswith("-") and not line.startswith("---"):
            cur["rm"].append(line[1:])
        elif cur is not None and line.startswith("+") and not line.startswith("+++"):
            cur["add"].append(line[1:])
    for p, hs in hunks.items():
        if not p or not is_test_path(p):
            continue
        tests_rm = sum(len(DEF_RE.findall(l)) for h in hs for l in h["rm"])
        tests_add = sum(len(DEF_RE.findall(l)) for h in hs for l in h["add"])
        if tests_rm > tests_add:
            out.append(Finding(p, hs[0]["line"], "deleted-test",
                               f"{tests_rm - tests_add} test definition(s) removed ({tests_rm} removed, {tests_add} added)"))
        for h in hs:
            for i, l in enumerate(h["add"]):
                if SKIP_RE.search(l):
                    out.append(Finding(p, h["line"] + i, "added-skip", f"skip/xfail added: `{l.strip()[:60]}`"))
            s_rm = sum(1 for l in h["rm"] if STRONG_LINE.search(l))
            s_add = sum(1 for l in h["add"] if STRONG_LINE.search(l))
            w_add = sum(1 for l in h["add"] if WEAK_LINE.search(l) and not STRONG_LINE.search(l))
            if s_rm > s_add and not (tests_rm > tests_add):
                out.append(Finding(p, h["line"], "loosened",
                                   f"strong assertions {s_rm} -> {s_add}"
                                   + (f", weak assertions +{w_add}" if w_add else "")))
    return out


# --------------------------------------------------------------------------- driver

def gather(paths):
    files = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if f.is_file() and not (set(f.parts) & SKIP_DIR_NAMES) and is_test_path(f.as_posix()) \
                        and f.suffix in (".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
                    files.append(f)
        elif p.is_file():
            files.append(p)
        else:
            raise SystemExit(f"error: {raw} does not exist")
    return files


_LINES: dict = {}


def _suppressed(f) -> bool:
    """A reviewed false positive: `# honest-tests: ignore` (or `// ...`) on the flagged line,
    the line above it, or the test's decorator/def line."""
    try:
        lines = _LINES.setdefault(f.path, Path(f.path).read_text(encoding="utf-8").splitlines())
    except OSError:
        return False
    for i in (f.line - 2, f.line - 1):
        if 0 <= i < len(lines) and "honest-tests: ignore" in lines[i]:
            return True
    return False


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", help="test files or directories")
    ap.add_argument("--diff", nargs="?", const="HEAD", metavar="BASE",
                    help="also check the test changes in git diff against BASE (default HEAD)")
    ap.add_argument("--group", choices=["all", "mock", "assertion"], default="all")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    if not args.paths and args.diff is None:
        ap.print_usage(sys.stderr)
        print("error: give test paths and/or --diff", file=sys.stderr)
        return 2
    try:
        findings, total, unscanned = [], 0, []
        for f in gather(args.paths):
            try:
                src = f.read_text(encoding="utf-8")
                fnd, n = scan_python(f.as_posix(), src) if f.suffix == ".py" else scan_js(f.as_posix(), src)
            except (SyntaxError, UnicodeDecodeError) as e:
                unscanned.append(f"{f.as_posix()} ({type(e).__name__})")
                continue
            findings += fnd
            total += n
        if args.diff is not None:
            findings += scan_diff(args.diff)
    except SystemExit as e:
        print(e, file=sys.stderr)
        return 2
    findings = [f for f in findings if not _suppressed(f)]
    if args.group != "all":
        findings = [f for f in findings if f.rule in GROUPS[args.group]]
    findings.sort(key=lambda f: (f.rule not in BAD, f.path, f.line))
    flagged = len({(f.path, f.test) for f in findings if f.test and f.rule in BAD})
    score = 10 if total == 0 else round(10 * (1 - flagged / total))
    if any(f.rule in ("deleted-test", "added-skip", "loosened") for f in findings):
        score = min(score, 6)
    if args.json:
        print(json.dumps({"tests_scanned": total, "tests_flagged": flagged, "scan_score": score,
                          "not_scanned": unscanned, "findings": [f.as_dict() for f in findings]}, indent=2))
    else:
        for label, want in (("BAD", True), ("SUSPECT", False)):
            group = [f for f in findings if (f.rule in BAD) == want]
            if group:
                print(f"{label} ({len(group)})")
                for f in group:
                    print(f"  {f.path}:{f.line}  [{f.rule}]  {f.msg}\n      fix: {ADVICE[f.rule]}")
        for u in unscanned:
            print(f"NOT SCANNED: {u}")
        print(f"\n{total} tests scanned, {flagged} flagged as unable to fail. Scan score: {score}/10"
              " (smell check only; run mutation_check.py to prove it)")
    return 1 if any(f.rule in BAD for f in findings) else 0


if __name__ == "__main__":
    sys.exit(main())
