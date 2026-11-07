#!/usr/bin/env python3
"""Diff-scoped mutation testing: prove that the tests actually notice a change.

Reads `git diff` to find the source lines you changed (test files are ignored), applies small
mutations to those lines only, one at a time, runs your test command for each mutant, and reports
which mutants were killed (a test failed) and which survived (every test still passed).
A survivor means no test would notice that bug.

    python mutation_check.py                          # diff against HEAD, auto-detect test command
    python mutation_check.py --base origin/main --cmd "pytest -q -x" --min-score 80
    python mutation_check.py --files pricing.py       # no git: mutate every line of these files

Python is mutated through `ast`; JavaScript/TypeScript through a comment/string-aware line mutator.
Standard library only. Python 3.9+. Source files are restored after every mutant, on errors, on
Ctrl+C and on SIGTERM. Exit codes: 0 ok, 1 score below --min-score, 2 usage/baseline problem.
"""
from __future__ import annotations

import argparse
import ast
import atexit
import copy
import fnmatch
import importlib.util
import json
import os
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

__version__ = "0.1.0"

EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
LANG_BY_EXT = {
    ".py": "py",
    ".js": "js", ".jsx": "js", ".mjs": "js", ".cjs": "js",
    ".ts": "js", ".tsx": "js", ".mts": "js", ".cts": "js",
}
SKIP_DIRS = {"node_modules", ".venv", "venv", "site-packages", "dist", "build", "__pycache__",
             ".git", ".tox", "coverage", "migrations"}
TEST_DIRS = {"tests", "test", "__tests__", "__mocks__", "fixtures", "e2e", "spec", "specs"}
INVALID_JS = re.compile(
    r"SyntaxError|Unexpected (?:token|identifier|end of|reserved|strict)|error TS\d+|"
    r"Transform failed|ParseError|Unterminated|Invalid or unexpected token")
KEYWORDS = {"if", "for", "while", "switch", "return", "catch", "function", "super", "typeof",
            "new", "throw", "else", "do", "try", "import", "require", "void", "delete"}
NOISE_CALL = re.compile(r"^(?:print|console\.\w+|logging\.\w+|logger\.\w+|log\.\w+|"
                        r"warnings\.warn|process\.stdout\.write)$")


# --------------------------------------------------------------------------- file selection

def is_test_file(rel: str) -> bool:
    p = rel.replace("\\", "/").lower()
    parts = p.split("/")
    name = parts[-1]
    for d in parts[:-1]:
        if d in TEST_DIRS or d.startswith("tests_") or d.startswith("test_"):
            return True
    if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
        return True
    if re.search(r"\.(test|spec)\.[a-z]+$", name) or re.search(r"_spec\.[a-z]+$", name):
        return True
    return False


def source_lang(rel: str) -> str | None:
    p = rel.replace("\\", "/")
    name = p.rsplit("/", 1)[-1]
    if name.endswith(".d.ts") or re.search(r"\.config\.[a-z]+$", name) or name == "setup.py":
        return None
    if any(d in SKIP_DIRS for d in p.split("/")[:-1]):
        return None
    return LANG_BY_EXT.get(os.path.splitext(name)[1].lower())


# --------------------------------------------------------------------------- git

def git(project: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(project), "-c", "core.quotepath=off", *args],
                          capture_output=True, text=True, encoding="utf-8", errors="replace")


def collect_changed(project: Path, base: str) -> dict:
    """Return {path relative to project: set of changed line numbers (new side) or None for all}."""
    top = git(project, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        raise SystemExit("error: not inside a git repository (use --files to name files explicitly)")
    if git(project, "rev-parse", "--verify", "-q", "HEAD").returncode != 0:
        ref = EMPTY_TREE  # repository has no commits yet
    elif base == "HEAD":
        ref = "HEAD"
    else:
        mb = git(project, "merge-base", base, "HEAD")
        if mb.returncode != 0 and git(project, "rev-parse", "--verify", "-q", base).returncode != 0:
            raise SystemExit(f"error: unknown --base ref {base!r}")
        ref = mb.stdout.strip() if mb.returncode == 0 else base
    diff = git(project, "diff", "--relative", "--no-renames", "--unified=0", "--no-color",
               "--no-ext-diff", ref, "--", ".")
    if diff.returncode != 0:
        raise SystemExit("error: git diff failed: " + diff.stderr.strip())
    out: dict = {}
    cur = None
    for line in diff.stdout.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip("\t")
            if path == "/dev/null":
                cur = None
            else:
                cur = path[2:] if path.startswith("b/") else path
                out.setdefault(cur, set())
        elif line.startswith("@@") and cur:
            m = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if m:
                start = int(m.group(1))
                count = int(m.group(2)) if m.group(2) is not None else 1
                out[cur].update(range(start, start + count))
    untracked = git(project, "ls-files", "--others", "--exclude-standard")
    for rel in untracked.stdout.splitlines():
        if rel.strip():
            out[rel.strip()] = None
    return out


# --------------------------------------------------------------------------- python mutator

PY_SYM = {
    ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.Eq: "==", ast.NotEq: "!=",
    ast.Is: "is", ast.IsNot: "is not", ast.In: "in", ast.NotIn: "not in",
    ast.And: "and", ast.Or: "or", ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/",
}
PY_CMP = {
    ast.Lt: [ast.GtE, ast.LtE], ast.LtE: [ast.Gt, ast.Lt], ast.Gt: [ast.LtE, ast.GtE],
    ast.GtE: [ast.Lt, ast.Gt], ast.Eq: [ast.NotEq], ast.NotEq: [ast.Eq], ast.Is: [ast.IsNot],
    ast.IsNot: [ast.Is], ast.In: [ast.NotIn], ast.NotIn: [ast.In],
}
PY_BOOL = {ast.And: ast.Or, ast.Or: ast.And}
PY_ARITH = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult}


def _call_name(call: ast.Call) -> str:
    f = call.func
    parts = []
    while isinstance(f, ast.Attribute):
        parts.append(f.attr)
        f = f.value
    if isinstance(f, ast.Name):
        parts.append(f.id)
    return ".".join(reversed(parts))


def py_sites(src: str, changed) -> list:
    tree = ast.parse(src)
    lines = src.split("\n")
    sites = []
    for idx, node in enumerate(ast.walk(tree)):
        ln = getattr(node, "lineno", None)
        if ln is None or (changed is not None and ln not in changed):
            continue
        if ln - 1 < len(lines) and "pragma: no mutate" in lines[ln - 1]:
            continue

        def add(kind, label, **kw):
            sites.append({"idx": idx, "line": ln, "kind": kind, "label": label, **kw})

        if isinstance(node, ast.Compare):
            for i, op in enumerate(node.ops):
                for new in PY_CMP.get(type(op), []):
                    add("cmp", f"`{PY_SYM[type(op)]}` -> `{PY_SYM[new]}`", i=i, new=new)
        elif isinstance(node, ast.BoolOp):
            new = PY_BOOL[type(node.op)]
            add("op", f"`{PY_SYM[type(node.op)]}` -> `{PY_SYM[new]}`", new=new)
        elif isinstance(node, (ast.BinOp, ast.AugAssign)) and type(node.op) in PY_ARITH:
            new = PY_ARITH[type(node.op)]
            aug = "=" if isinstance(node, ast.AugAssign) else ""
            add("op", f"`{PY_SYM[type(node.op)]}{aug}` -> `{PY_SYM[new]}{aug}`", new=new)
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            add("not", "remove `not`")
        elif isinstance(node, ast.Constant):
            v = node.value
            if isinstance(v, bool):
                add("const", f"`{v}` -> `{not v}`", value=not v)
            elif isinstance(v, int):
                add("const", f"`{v}` -> `{v + 1}`", value=v + 1)
                add("const", f"`{v}` -> `{v - 1}`", value=v - 1)
            elif isinstance(v, float):
                add("const", f"`{v!r}` -> `{v + 1!r}`", value=v + 1)
        elif isinstance(node, ast.Return):
            if node.value is not None and not (isinstance(node.value, ast.Constant)
                                               and node.value.value is None):
                add("ret", "`return <value>` -> `return None`")
        elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            if not NOISE_CALL.match(_call_name(node.value)):
                add("rmcall", "remove call statement")
        elif isinstance(node, ast.If) and not isinstance(node.test, (ast.Compare, ast.UnaryOp)):
            add("negif", "negate `if` condition")
    return sites


def _replace_child(nodes, target, new):
    for p in nodes:
        for field, val in ast.iter_fields(p):
            if isinstance(val, list):
                for i, item in enumerate(val):
                    if item is target:
                        val[i] = new
                        return
            elif val is target:
                setattr(p, field, new)
                return


def py_apply(tree, site) -> str:
    nodes = list(ast.walk(tree))
    n = nodes[site["idx"]]
    k = site["kind"]
    if k == "cmp":
        n.ops[site["i"]] = site["new"]()
    elif k == "op":
        n.op = site["new"]()
    elif k == "const":
        n.value = site["value"]
    elif k == "ret":
        n.value = ast.Constant(value=None)
    elif k == "not":
        _replace_child(nodes, n, n.operand)
    elif k == "rmcall":
        _replace_child(nodes, n, ast.copy_location(ast.Pass(), n))
    elif k == "negif":
        n.test = ast.UnaryOp(op=ast.Not(), operand=n.test)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree) + "\n"


# --------------------------------------------------------------------------- javascript mutator

def mask_js(lines: list) -> list:
    """Blank out comments, strings and template literals (same length) so regexes only see code."""
    masked = []
    in_block = in_tpl = False
    for line in lines:
        out = []
        i, n, quote = 0, len(line), None
        while i < n:
            c = line[i]
            if in_block:
                if line.startswith("*/", i):
                    out.append("__"); i += 2; in_block = False
                else:
                    out.append("_"); i += 1
            elif in_tpl or quote:
                if c == "\\":
                    out.append("_" * min(2, n - i)); i += 2
                elif (in_tpl and c == "`") or (quote and c == quote):
                    out.append("_"); i += 1; in_tpl = False; quote = None
                else:
                    out.append("_"); i += 1
            elif line.startswith("//", i):
                out.append("_" * (n - i)); i = n
            elif line.startswith("/*", i):
                out.append("__"); i += 2; in_block = True
            elif c in "\"'":
                quote = c; out.append("_"); i += 1
            elif c == "`":
                in_tpl = True; out.append("_"); i += 1
            else:
                out.append(c); i += 1
        masked.append("".join(out))
    return masked


def _balanced(s: str) -> bool:
    depth = 0
    for ch in s:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


JS_TOKEN_RULES = [
    (re.compile(r"(?<=\s)(===|!==|==|!=)(?=\s)"),
     {"===": ["!=="], "!==": ["==="], "==": ["!="], "!=": ["=="]}),
    (re.compile(r"(?<=\s)(<=|>=|<|>)(?=\s)"),
     {"<": [">=", "<="], "<=": [">", "<"], ">": ["<=", ">="], ">=": ["<", ">"]}),
    (re.compile(r"(?<=\s)(&&|\|\|)(?=\s)"), {"&&": ["||"], "||": ["&&"]}),
    (re.compile(r"(?<=\s)(\+=|-=)(?=\s)"), {"+=": ["-="], "-=": ["+="]}),
    (re.compile(r"(?<=\s)([+\-*/])(?=\s)"), {"+": ["-"], "-": ["+"], "*": ["/"], "/": ["*"]}),
    (re.compile(r"\b(true|false)\b"), {"true": ["false"], "false": ["true"]}),
]
JS_NUM = re.compile(r"(?<![\w.$#])(\d+(?:\.\d+)?)(?![\w.])")
JS_IF = re.compile(r"^(\s*(?:\}\s*else\s+)?if\s*)\((.*)\)(\s*\{?\s*)$")
JS_RETURN = re.compile(r"^(\s*)return\s+(.*?)\s*;?\s*$")
JS_CALL = re.compile(r"^(\s*)(?:await\s+)?([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\(.*\)\s*;?\s*$")
JS_IMPORT = re.compile(r"^\s*(?:import\b|export\s+(?:\*|\{[^}]*\})\s*from\b|.*\brequire\()")


def js_sites(src: str, changed) -> list:
    raw = src.split("\n")
    lines = [l[:-1] if l.endswith("\r") else l for l in raw]
    masked = mask_js(lines)
    sites = []
    for i, (orig, m) in enumerate(zip(lines, masked)):
        ln = i + 1
        if changed is not None and ln not in changed:
            continue
        if not m.strip() or "pragma: no mutate" in orig or JS_IMPORT.match(m):
            continue
        found = []

        def splice(a, b, text, label):
            found.append((label, orig[:a] + text + orig[b:]))

        for rx, table in JS_TOKEN_RULES:
            for mt in rx.finditer(m):
                for new in table[mt.group(1)]:
                    splice(mt.start(1), mt.end(1), new, f"`{mt.group(1)}` -> `{new}`")
        for mt in JS_NUM.finditer(m):
            v = mt.group(1)
            if "." in v:
                splice(mt.start(1), mt.end(1), repr(float(v) + 1), f"`{v}` -> `{float(v) + 1!r}`")
            else:
                splice(mt.start(1), mt.end(1), str(int(v) + 1), f"`{v}` -> `{int(v) + 1}`")
                splice(mt.start(1), mt.end(1), str(int(v) - 1), f"`{v}` -> `{int(v) - 1}`")
        mt = JS_IF.match(m)
        if mt and _balanced(mt.group(2)) and mt.group(2).strip():
            a, b = mt.start(2), mt.end(2)
            found.append(("negate `if` condition", orig[:a] + "!(" + orig[a:b] + ")" + orig[b:]))
        mt = JS_RETURN.match(m)
        if mt and mt.group(2) and _balanced(mt.group(2)) \
                and not re.search(r"[+\-*/&|?:=,(<>]$", mt.group(2)) \
                and mt.group(2) not in ("undefined", "null"):
            found.append(("`return <value>` -> `return undefined`",
                          orig[:mt.end(1)] + "return undefined;"))
        mt = JS_CALL.match(m)
        if mt and mt.group(2) not in KEYWORDS and not NOISE_CALL.match(mt.group(2)) \
                and _balanced(m):
            found.append(("remove call statement", orig[:mt.end(1)] + ";"))
        seen, uniq = set(), []
        for label, new_line in found:
            if (label, new_line) not in seen and new_line != orig:
                seen.add((label, new_line))
                uniq.append((label, new_line))
        counts = {}
        for label, _ in uniq:
            counts[label] = counts.get(label, 0) + 1
        nth = {}
        for label, new_line in uniq:
            nth[label] = nth.get(label, 0) + 1
            shown = f"{label} (#{nth[label]})" if counts[label] > 1 else label
            sites.append({"line": ln, "label": shown, "kind": "line", "new_line": new_line})
    return sites


def js_apply(src: str, site) -> str:
    raw = src.split("\n")
    i = site["line"] - 1
    eol = "\r" if raw[i].endswith("\r") else ""
    raw[i] = site["new_line"] + eol
    return "\n".join(raw)


# --------------------------------------------------------------------------- running tests

def kill_tree(p: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            os.killpg(os.getpgid(p.pid), signal.SIGKILL)
    except Exception:
        try:
            p.kill()
        except Exception:
            pass


def run_cmd(cmd: str, cwd: Path, timeout: float):
    """Return (returncode or None on timeout, combined output, timed_out)."""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", CI="1", NO_COLOR="1", FORCE_COLOR="0",
               PYTHONIOENCODING="utf-8")
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" \
        else {"start_new_session": True}
    p = subprocess.Popen(cmd, shell=True, cwd=str(cwd), stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, **kw)
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, out.decode("utf-8", "replace"), False
    except subprocess.TimeoutExpired:
        kill_tree(p)
        try:
            out, _ = p.communicate(timeout=5)
        except Exception:
            out = b""
        return None, out.decode("utf-8", "replace"), True
    except BaseException:
        kill_tree(p)
        raise


def count_tests(out: str) -> int | None:
    for rx in (r"(\d+) passed", r"Ran (\d+) tests?", r"(?:#|ℹ) pass (\d+)",
               r"Tests:\s+(?:\d+ \w+, )*(\d+) passed", r"(\d+) passing"):
        m = re.search(rx, out)
        if m:
            return int(m.group(1))
    return None


def detect_cmd(project: Path) -> str | None:
    pkg = project / "package.json"
    if pkg.exists():
        try:
            scripts = json.loads(pkg.read_text(encoding="utf-8")).get("scripts", {})
            if scripts.get("test") and "no test specified" not in scripts["test"]:
                return "npm test --silent"
        except (OSError, ValueError):
            pass
    py = sys.executable if " " not in sys.executable else f'"{sys.executable}"'
    has_py_tests = any(project.glob("test_*.py")) or any(project.glob("tests/**/test_*.py")) \
        or any(project.glob("tests/**/*_test.py")) or (project / "conftest.py").exists()
    if has_py_tests or (project / "pytest.ini").exists():
        if importlib.util.find_spec("pytest"):
            return f"{py} -m pytest -q -x"
        return f"{py} -m unittest discover -f" + (" -s tests" if (project / "tests").is_dir() else "")
    return None


# --------------------------------------------------------------------------- safe file handling

class Guard:
    """Snapshot files, write mutants, and always put the originals back (bytes and mtime)."""

    def __init__(self):
        self.orig: dict = {}
        self.tmp = Path(tempfile.mkdtemp(prefix="honest-tests-backup-"))
        self.n = 0
        atexit.register(self.restore_all)

    def snapshot(self, path: Path) -> bytes:
        st = path.stat()
        data = path.read_bytes()
        self.orig[path] = (data, st.st_atime_ns, st.st_mtime_ns)
        self.n += 1
        (self.tmp / f"{self.n}_{path.name}").write_bytes(data)  # last resort if we are SIGKILLed
        return data

    def write(self, path: Path, text: str, bump: int) -> None:
        data, _, mtime = self.orig[path]
        path.write_bytes(text.encode("utf-8"))
        os.utime(path, ns=(mtime + bump * 10**9, mtime + bump * 10**9))  # defeat .pyc/cache reuse

    def restore(self, path: Path) -> bool:
        data, atime, mtime = self.orig[path]
        path.write_bytes(data)
        os.utime(path, ns=(atime, mtime))
        return path.read_bytes() == data

    def restore_all(self) -> bool:
        ok = True
        for p in list(self.orig):
            try:
                ok = self.restore(p) and ok
            except OSError:
                ok = False
        if ok:
            self.orig.clear()  # nothing left to restore (also makes the atexit hook a no-op)
            shutil.rmtree(self.tmp, ignore_errors=True)
        else:
            sys.stderr.write(f"WARNING: could not restore every file; originals are in {self.tmp}\n")
        return ok


# --------------------------------------------------------------------------- main

def trunc(s: str, n: int = 78) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 3] + "..."


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--base", default="HEAD", help="git ref to diff against (default HEAD; "
                    "a branch such as origin/main uses the merge-base)")
    ap.add_argument("--cmd", help="test command (auto-detected from pytest/unittest/package.json)")
    ap.add_argument("--project", default=".", help="directory to run in (default cwd)")
    ap.add_argument("--files", nargs="+", help="mutate every line of these files instead of the diff")
    ap.add_argument("--min-score", type=float, default=80.0,
                    help="exit 1 if the mutation score is below this percent (default 80)")
    ap.add_argument("--timeout", type=float, help="seconds per mutant (default max(10, 3x baseline))")
    ap.add_argument("--max-mutants", type=int, default=80, help="sample at most this many (default 80)")
    ap.add_argument("--seed", type=int, default=0, help="sampling seed (default 0)")
    ap.add_argument("--include", action="append", default=[], help="only files matching this glob")
    ap.add_argument("--exclude", action="append", default=[], help="skip files matching this glob")
    ap.add_argument("--list", action="store_true", help="list mutants without running tests")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("-v", "--verbose", action="store_true", help="also list killed mutants")
    ap.add_argument("--version", action="version", version=__version__)
    args = ap.parse_args(argv)

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    project = Path(args.project).resolve()
    if not project.is_dir():
        print(f"error: --project {project} is not a directory", file=sys.stderr)
        return 2
    if args.files:
        changed = {f.replace("\\", "/"): None for f in args.files}
        base_desc = "--files (all lines)"
    else:
        try:
            changed = collect_changed(project, args.base)
        except SystemExit as e:
            print(e, file=sys.stderr)
            return 2
        base_desc = args.base

    targets, ignored_tests, ignored_other = [], [], []
    for rel, lines in sorted(changed.items()):
        path = (project / rel).resolve()
        lang = source_lang(rel)
        if args.files is None and is_test_file(rel):
            ignored_tests.append(rel)
        elif lang is None or not path.is_file():
            ignored_other.append(rel)
        elif args.include and not any(fnmatch.fnmatch(rel, g) for g in args.include):
            continue
        elif any(fnmatch.fnmatch(rel, g) for g in args.exclude):
            continue
        else:
            targets.append((rel, path, lang, lines))

    log = (lambda *a: None) if args.json else print
    log("honest-tests / mutation-check")
    log(f"  project : {project}")
    log(f"  base    : {base_desc}")
    log(f"  files   : {len(targets)} source file(s) with changes"
        + (f"; ignored {len(ignored_tests)} test file(s)" if ignored_tests else ""))

    if not targets:
        log("\nNothing to mutate: no changed source lines (test-only or empty diff).")
        log("If you changed source files, check --base / --project, or pass --files.")
        if args.json:
            print(json.dumps({"score": None, "mutants": [], "note": "no changed source lines"}))
        return 0

    cmd = args.cmd or detect_cmd(project)
    if not cmd:
        print("error: could not detect a test command; pass --cmd", file=sys.stderr)
        return 2
    log(f"  command : {cmd}")

    # Baseline: the suite must be green first, or the score means nothing.
    t0 = time.time()
    code, out, timed_out = run_cmd(cmd, project, args.timeout or 600)
    base_secs = time.time() - t0
    if timed_out or code != 0:
        print("\nBASELINE FAILED: the test command does not pass on the unmutated code.\n"
              "Fix the tests first; a mutation score is meaningless on a red suite.\n", file=sys.stderr)
        print("\n".join(out.strip().splitlines()[-25:]), file=sys.stderr)
        return 2
    n_tests = count_tests(out)
    timeout = args.timeout or max(10.0, base_secs * 3)
    log(f"  baseline: PASS" + (f", {n_tests} tests" if n_tests is not None else "")
        + f" in {base_secs:.1f}s (mutant timeout {timeout:.0f}s)")

    # Enumerate mutants.
    mutants = []
    skipped_files = []
    for rel, path, lang, lines in targets:
        try:
            src = path.read_bytes().decode("utf-8")
            sites = py_sites(src, lines) if lang == "py" else js_sites(src, lines)
        except (SyntaxError, UnicodeDecodeError, ValueError) as e:
            skipped_files.append((rel, str(e).splitlines()[0] if str(e) else type(e).__name__))
            continue
        srclines = src.split("\n")
        for s in sites:
            s.update(file=rel, path=path, lang=lang,
                     source=srclines[s["line"] - 1] if s["line"] <= len(srclines) else "")
            mutants.append(s)
    for rel, why in skipped_files:
        log(f"  skipped : {rel} ({why})")
    total_found = len(mutants)
    if total_found > args.max_mutants:
        mutants = random.Random(args.seed).sample(mutants, args.max_mutants)
    mutants.sort(key=lambda s: (s["file"], s["line"], s["label"]))
    log(f"  mutants : {len(mutants)}"
        + (f" (sampled from {total_found}; raise --max-mutants for all)" if total_found > len(mutants) else ""))

    if not mutants:
        log("\nNo mutable constructs on the changed lines (comments, strings, imports, config).")
        if args.json:
            print(json.dumps({"score": None, "mutants": [], "note": "no mutable constructs"}))
        return 0
    if args.list:
        for s in mutants:
            log(f"  {s['file']}:{s['line']}  {s['label']:<34}  {trunc(s['source'], 60)}")
        return 0

    guard = Guard()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    node_check = False
    results = []
    interrupted = False
    try:
        parsed = {}
        node = shutil.which("node")
        for rel, path, lang, _ in targets:
            guard.snapshot(path)
            if lang == "py":
                try:
                    parsed[path] = ast.parse(guard.orig[path][0].decode("utf-8"))
                except SyntaxError:
                    parsed[path] = None
        if node and any(s["lang"] == "js" and s["path"].suffix in (".js", ".cjs", ".mjs") for s in mutants):
            node_check = True
        started = time.time()
        for i, s in enumerate(mutants, 1):
            path = s["path"]
            original = guard.orig[path][0].decode("utf-8")
            status = "survived"
            try:
                if s["lang"] == "py":
                    mutated = py_apply(copy.deepcopy(parsed[path]), s)
                    try:
                        compile(mutated, str(path), "exec")
                    except (SyntaxError, ValueError):
                        status = "invalid"
                else:
                    mutated = js_apply(original, s)
                if status != "invalid":
                    guard.write(path, mutated, i)
                    if s["lang"] == "js" and node_check and path.suffix in (".js", ".cjs", ".mjs"):
                        chk = subprocess.run([node, "--check", str(path)], capture_output=True)
                        if chk.returncode != 0:
                            status = "invalid"
                    if status != "invalid":
                        code, out, timed_out = run_cmd(cmd, project, timeout)
                        if timed_out:
                            status = "timeout"
                        elif code == 0:
                            status = "survived"
                        elif s["lang"] == "js" and INVALID_JS.search(out):
                            status = "invalid"
                        else:
                            status = "killed"
            finally:
                guard.restore(path)
            results.append((s, status))
            if sys.stderr.isatty() and not args.json:
                sys.stderr.write(f"\r  testing mutant {i}/{len(mutants)} "
                                 f"({time.time() - started:.0f}s)   ")
                sys.stderr.flush()
    except KeyboardInterrupt:
        interrupted = True
    finally:
        restored = guard.restore_all()
        if sys.stderr.isatty() and not args.json:
            sys.stderr.write("\r" + " " * 60 + "\r")

    survived = [(s, st) for s, st in results if st == "survived"]
    killed = [(s, st) for s, st in results if st in ("killed", "timeout")]
    invalid = [(s, st) for s, st in results if st == "invalid"]
    valid = len(survived) + len(killed)
    score = round(100 * len(killed) / valid) if valid else None

    if args.json:
        print(json.dumps({
            "base": base_desc, "command": cmd, "baseline_tests": n_tests, "restored": restored,
            "interrupted": interrupted, "score": score, "killed": len(killed),
            "survived": len(survived), "invalid": len(invalid), "min_score": args.min_score,
            "mutants": [{"file": s["file"], "line": s["line"], "mutation": s["label"],
                         "source": s["source"].strip(), "status": st} for s, st in results],
        }, indent=2))
    else:
        if survived:
            print(f"\nSURVIVED ({len(survived)}): every test still passed with this bug in place")
            for s, _ in survived:
                print(f"  {s['file']}:{s['line']:<4} {s['label']:<34} | {trunc(s['source'], 52)}")
        if args.verbose and killed:
            print(f"\nKILLED ({len(killed)})")
            for s, st in killed:
                print(f"  {s['file']}:{s['line']:<4} {s['label']:<34} | {trunc(s['source'], 52)}"
                      + ("  [timeout]" if st == "timeout" else ""))
        if invalid:
            print(f"\n({len(invalid)} mutant(s) did not compile and were not counted)")
        print()
        tests = f"{n_tests} tests pass, " if n_tests is not None else ""
        if score is None:
            print("Mutation score: n/a (no valid mutants)")
        else:
            verdict = "PASS" if score >= args.min_score else "FAIL"
            print(f"{tests}{len(survived)} of {valid} mutants survived. "
                  f"Mutation score: {score}% (min {args.min_score:g}%) -> {verdict}")
        print("Source files restored: " + ("yes, byte-identical" if restored else "NO - see warning"))
        if survived:
            print("\nNext: for each survivor, write or tighten a test that fails when that exact "
                  "change is applied,\nthen re-run. Survivors that change nothing observable "
                  "(equivalent mutants) can be\nmarked `# pragma: no mutate`; say so explicitly.")
    if interrupted:
        print("Interrupted. Files restored." if restored else "Interrupted.", file=sys.stderr)
        return 130
    if score is not None and score < args.min_score:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
