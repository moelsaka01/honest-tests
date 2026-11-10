#!/usr/bin/env python3
"""Diff-scoped mutation testing: prove that the tests actually notice a change.

Reads `git diff` to find the source lines you changed (test files are ignored), applies small
mutations to those lines only, one at a time, runs your test command for each mutant, and reports
which mutants were killed (a test failed) and which survived (every test still passed).
A survivor means no test would notice that bug.

    python mutation_check.py                          # diff against HEAD, auto-detect test command
    python mutation_check.py --base origin/main --cmd "pytest -q -x" --min-score 80
    python mutation_check.py --files pricing.py       # no git: mutate every line of these files

Python is mutated through `ast`; JavaScript/TypeScript through a tokenizer with bracket and statement structure.
Standard library only. Python 3.9+. Source files are restored after every mutant, on errors, on
Ctrl+C and on SIGTERM; after a SIGKILL run `--restore`. Provably equivalent mutants (identical
bytecode) are skipped; others can be accepted with a reason in `.mutation-ignore`.
Exit codes: 0 ok, 1 score below --min-score, 2 usage/baseline problem.
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
import threading
import time
import queue
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

__version__ = "0.3.0"

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
# A real tokenizer (strings, nested template literals, regex-vs-division, numbers, comments) with
# bracket matching and statement boundaries (including automatic semicolon insertion). It is not a
# complete ECMAScript/TypeScript grammar (see the README); every mutant is still validated before it
# is allowed to count.

TS_EXT = {".ts", ".tsx", ".mts", ".cts"}
_JS_PUNCT = sorted(
    "... === !== **= <<= &&= ||= ??= => == != <= >= && || ?? ?. ++ -- += -= *= /= %= &= |= ^= << ** "
    "( ) [ ] { } ; , < > + - * / % & | ^ ! ~ ? : = . @".split(), key=len, reverse=True)
_RE_ID = re.compile(r"[A-Za-z_$#\u0080-￿][\w$\u0080-￿]*")
_RE_NUM = re.compile(r"0[xX][\da-fA-F_]+n?|0[bB][01_]+n?|0[oO][0-7_]+n?|"
                     r"(?:\d[\d_]*(?:\.[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d+)?n?")
_REGEX_AFTER = {"return", "typeof", "instanceof", "in", "of", "new", "delete", "void", "throw", "case",
                "do", "else", "yield", "await"}
_OPERATOR_WORDS = _REGEX_AFTER | {"import", "export", "function", "extends", "as", "satisfies", "default"}
_TYPE_WORDS = {"void", "string", "number", "boolean", "any", "unknown", "never", "object", "bigint",
               "symbol"}
_BIN = {"===": ["!=="], "!==": ["==="], "==": ["!="], "!=": ["=="],
        "<": [">=", "<="], "<=": [">", "<"], ">": ["<=", ">="], ">=": ["<", ">"],
        "&&": ["||"], "||": ["&&"], "+=": ["-="], "-=": ["+="],
        "+": ["-"], "-": ["+"], "*": ["/"], "/": ["*"], "%": ["*"]}
_NEEDS_OPERANDS = {"<", "<=", ">", ">=", "+", "-", "*", "/", "%"}
_CONT_START = {".", "?.", "?", ":", "&&", "||", "??", "==", "===", "!=", "!==", "<", ">", "<=", ">=", "*",
               "/", "%", "=", "+=", "-=", "*=", "/=", "%=", "|", "&", "^", "+", "-", "(", "[", ",",
               "=>", "**"}
_GENERIC_OK = {",", ".", "[", "]", "|", "&", "?", ":", "<", ">", "=>", "(", ")", "{", "}", "...", "*", "="}
_GENERIC_AFTER = {"(", ")", ",", ";", "=", "[", ".", "{", "|", "&", ":", ">", "=>", "?", "}"}


class _Tok:
    __slots__ = ("k", "t", "a", "b", "line", "nl")

    def __init__(self, k, t, a, b, line, nl):
        self.k, self.t, self.a, self.b, self.line, self.nl = k, t, a, b, line, nl


def js_lex(src: str) -> list:
    """Tokens: id, num, str, re, tpl (template chunk) and p (punctuator). Comments are dropped."""
    toks, stack = [], []
    i, n, line, nl = 0, len(src), 1, False

    def regex_ok():
        if not toks:
            return True
        p = toks[-1]
        if p.k == "p":
            return p.t not in (")", "]", "}")
        if p.k == "id":
            return p.t in _REGEX_AFTER
        if p.k == "tpl":
            return not p.t.endswith("`")
        return False

    def tpl_chunk(j):
        while j < n:
            ch = src[j]
            if ch == "\\":
                j += 2
            elif ch == "`":
                return min(j + 1, n), True
            elif ch == "$" and src.startswith("${", j):
                return j + 2, False
            else:
                j += 1
        return n, True

    def emit(kind, a, b):
        nonlocal line, nl
        text = src[a:b]
        toks.append(_Tok(kind, text, a, b, line, nl))
        nl = False
        line += text.count("\n")

    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            nl = True
            i += 1
        elif c.isspace() or c == "﻿":
            i += 1
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            if "\n" in src[i:j]:
                nl = True
                line += src.count("\n", i, j)
            i = j
        elif c in "'\"":
            j = i + 1
            while j < n and src[j] != c and src[j] != "\n":
                j += 2 if src[j] == "\\" else 1
            j = j + 1 if j < n and src[j] == c else min(j, n)
            emit("str", i, j)
            i = j
        elif c == "`":
            j, done = tpl_chunk(i + 1)
            emit("tpl", i, j)
            if not done:
                stack.append("t")
            i = j
        elif c == "}" and stack and stack[-1] == "t":
            stack.pop()
            j, done = tpl_chunk(i + 1)
            emit("tpl", i, j)
            if not done:
                stack.append("t")
            i = j
        elif c == "/" and regex_ok() and _regex_end(src, i) > 0:
            j = _regex_end(src, i)
            emit("re", i, j)
            i = j
        elif c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit()):
            m = _RE_NUM.match(src, i)
            emit("num", i, m.end())
            i = m.end()
        elif _RE_ID.match(src, i):
            m = _RE_ID.match(src, i)
            emit("id", i, m.end())
            i = m.end()
        else:
            for p in _JS_PUNCT:
                if src.startswith(p, i) and not (p == "?." and i + 2 < n and src[i + 2].isdigit()):
                    break
            else:
                p = c
            if p == "{":
                stack.append("b")
            elif p == "}" and stack:
                stack.pop()
            emit("p", i, i + len(p))
            i += len(p)
    return toks


def _regex_end(src: str, i: int) -> int:
    """End offset of a regex literal starting at src[i] == '/', or 0 if it is not one."""
    n, j, in_class = len(src), i + 1, False
    while j < n and src[j] != "\n":
        ch = src[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "[":
            in_class = True
        elif ch == "]":
            in_class = False
        elif ch == "/" and not in_class:
            j += 1
            while j < n and src[j].isalpha():
                j += 1
            return j if j > i + 2 else 0
        j += 1
    return 0


def _opens(t) -> bool:
    return (t.k == "p" and t.t in ("(", "[", "{")) or (t.k == "tpl" and t.t.endswith("${"))


def _closes(t) -> bool:
    return (t.k == "p" and t.t in (")", "]", "}")) or (t.k == "tpl" and t.t.startswith("}"))


def _pairs(toks) -> dict:
    match, st = {}, []
    for i, t in enumerate(toks):
        if _closes(t) and st:
            j = st.pop()
            match[i], match[j] = j, i
        if _opens(t):
            st.append(i)
    return match


def _ends_expr(p, brace: bool = False) -> bool:
    """Can token p be the last token of an operand (so that a following operator is binary)?"""
    if p.k in ("num", "str", "re"):
        return True
    if p.k == "tpl":
        return p.t.endswith("`")
    if p.k == "id":
        return p.t not in _OPERATOR_WORDS
    return p.t in (")", "]", "++", "--") or (brace and p.t == "}")


def _starts_operand(t) -> bool:
    if t.k in ("num", "str", "re", "id"):
        return True
    if t.k == "tpl":
        return t.t.startswith("`")
    return t.t in ("(", "[", "{", "!", "~", "-", "+", "++", "--")


def _starts_cont(t) -> bool:
    """Does t, at the start of a line, continue the previous expression (no semicolon inserted)?"""
    if t.k == "p":
        return t.t in _CONT_START
    if t.k == "id":
        return t.t in ("instanceof", "in", "as", "satisfies", "of")
    return t.k == "tpl" and t.t.startswith("`")


def _continues(prev, t) -> bool:
    return not _ends_expr(prev, True) or _starts_cont(t)


def _expr_end(toks, j: int, stop_comma: bool = False) -> int:
    """Index one past the expression that starts at toks[j] (stops at ;, an unmatched closer, a
    top-level comma when asked, or a line break that automatic semicolon insertion would split)."""
    d, k = 0, j
    while k < len(toks):
        t = toks[k]
        if _closes(t):
            if d == 0:
                return k
            d -= 1
        elif d == 0:
            if t.k == "p" and (t.t == ";" or (stop_comma and t.t == ",")):
                return k
            if k > j and t.nl and not _continues(toks[k - 1], t):
                return k
        if _opens(t):
            d += 1
        k += 1
    return k


def _case_colon(toks, match, ci: int) -> bool:
    k = ci - 1
    while k >= 0:
        t = toks[k]
        if _closes(t) and k in match:
            k = match[k] - 1
            continue
        if _opens(t) or (t.k == "p" and t.t in (";", "{", "}", "?")):
            return False
        if t.k == "id" and t.t in ("case", "default"):
            return True
        k -= 1
    return False


def _stmt_start(toks, match, i: int) -> bool:
    if i == 0:
        return True
    p, t = toks[i - 1], toks[i]
    if p.k == "p" and p.t in (";", "{", "}"):
        return True
    if p.k == "p" and p.t == ")":
        o = match.get(i - 1)
        if o and toks[o - 1].k == "id" and toks[o - 1].t in ("if", "for", "while", "with"):
            return True
    if p.k == "id" and p.t in ("else", "do"):
        return True
    if p.k == "p" and p.t == ":" and _case_colon(toks, match, i - 1):
        return True
    return t.nl and not _continues(p, t)


def _skipped(toks, match, ts: bool) -> set:
    """Token indexes that are not runtime code: imports, re-exports, and TypeScript-only statements."""
    skip = set()
    for i, t in enumerate(toks):
        if t.k != "id" or i in skip or not _stmt_start(toks, match, i):
            continue
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        nxt2 = toks[i + 2] if i + 2 < len(toks) else None
        hit = False
        if t.t == "import":
            hit = nxt is not None and not (nxt.k == "p" and nxt.t in ("(", "."))
        elif t.t == "export":
            hit = nxt is not None and ((nxt.k == "p" and nxt.t in ("*", "{")) or
                                       (ts and nxt.k == "id" and nxt.t in ("type", "interface", "declare")))
        elif ts and t.t in ("interface", "declare"):
            hit = nxt is not None and nxt.k == "id"
        elif ts and t.t == "type":
            hit = nxt is not None and nxt.k == "id" and nxt2 is not None and nxt2.t in ("=", "<")
        if hit:
            skip.update(range(i, _expr_end(toks, i)))
    return skip


def _generics(toks) -> set:
    """TypeScript type-argument brackets such as Array<number> (so < and > are not mutated as comparisons)."""
    out, n = set(), len(toks)
    for i, t in enumerate(toks):
        if i == 0 or i in out or not (t.k == "p" and t.t == "<"):
            continue
        p = toks[i - 1]
        if not (p.k == "id" and p.b == t.a):
            continue
        d, k, found = 0, i, None
        while k < n:
            u = toks[k]
            if u.k == "p" and u.t == "<":
                d += 1
            elif u.k == "p" and u.t == ">":
                d -= 1
                if d == 0:
                    found = k
                    break
            elif not (u.k in ("id", "num", "str") or (u.k == "p" and u.t in _GENERIC_OK)):
                break
            k += 1
        if found is None:
            continue
        after = toks[found + 1] if found + 1 < n else None
        if after is None or after.nl or (after.k == "p" and after.t in _GENERIC_AFTER):
            out.update(x for x in range(i, found + 1) if toks[x].k == "p" and toks[x].t in ("<", ">"))
    return out


def _call_stmt(toks, match, i: int):
    """If a call statement (`[await] a.b.c(...)`) starts at toks[i], return (dotted name, index of `)`)."""
    j = i + 1 if toks[i].t == "await" else i
    parts = []
    while j < len(toks) and toks[j].k == "id":
        parts.append(toks[j].t)
        j += 1
        if j < len(toks) and toks[j].k == "p" and toks[j].t in (".", "?."):
            parts.append(".")
            j += 1
        else:
            break
    if not parts or parts[-1] == "." or j >= len(toks) or toks[j].t != "(" or j not in match:
        return None
    m = match[j]
    nxt = toks[m + 1] if m + 1 < len(toks) else None
    if nxt is not None and not (nxt.k == "p" and nxt.t in (";", "}")) and not (nxt.nl and not _starts_cont(nxt)):
        return None
    return "".join(parts), m


def js_sites(src: str, changed, ts: bool = False, exclude=None, blanked=None) -> list:
    """exclude: exact (start, end) character ranges that are not runtime code, from a real parser."""
    if blanked is not None and len(blanked) == len(src):
        toks, exclude = js_lex(blanked), ()
    elif exclude is None:
        toks = js_lex(src)
    else:  # blank out types, declarations and JSX markup (same length, offsets unchanged) before lexing
        chars = list(src)
        for a, b in exclude:
            for k in range(max(a, 0), min(b, len(chars))):
                if chars[k] != "\n":
                    chars[k] = " "
        toks = js_lex("".join(chars))
    match = _pairs(toks)
    lines = src.split("\n")
    skip = _skipped(toks, match, ts and exclude is None)
    generic = _generics(toks) if ts and exclude is None else set()
    ts = ts and exclude is None
    for_hdr = set()
    for i, t in enumerate(toks):
        if t.k == "id" and t.t == "for" and i + 1 < len(toks) and (i + 1) in match:
            for_hdr.update(range(i + 1, match[i + 1] + 1))
    cands = []

    def add(line, label, edits):
        if changed is not None and line not in changed:
            return
        if line <= len(lines) and "pragma: no mutate" in lines[line - 1]:
            return
        cands.append((line, label, tuple(edits)))

    def body_edit(first, end):
        text = src[toks[first].a:toks[end - 1].b].strip()
        if text in ("undefined", "null", "void 0"):
            return None
        return (toks[first].a, toks[end - 1].b, "undefined")

    for i, t in enumerate(toks):
        if i in skip:
            continue
        prev = toks[i - 1] if i else None
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if t.k == "p":
            op = t.t
            if op in _BIN:
                if i in generic:
                    continue
                if op in _NEEDS_OPERANDS and not (prev and _ends_expr(prev) and nxt and _starts_operand(nxt)):
                    continue
                for new in _BIN[op]:
                    add(t.line, f"`{op}` -> `{new}`", [(t.a, t.b, new)])
            elif op == "!" and (prev is None or not _ends_expr(prev, True)) and nxt and _starts_operand(nxt):
                add(t.line, "remove `!`", [(t.a, t.b, "")])
            elif op in ("++", "--") and i not in for_hdr:
                new = "--" if op == "++" else "++"
                add(t.line, f"`{op}` -> `{new}`", [(t.a, t.b, new)])
            elif op == "=>" and nxt and not (nxt.k == "p" and nxt.t == "{") \
                    and not (ts and nxt.k == "id" and nxt.t in _TYPE_WORDS):
                e = _expr_end(toks, i + 1, True)
                edit = body_edit(i + 1, e) if e > i + 1 else None
                if edit:
                    add(t.line, "arrow body -> `undefined`", [edit])
        elif t.k == "num":
            v = t.t
            plain = re.fullmatch(r"\d+(?:\.\d+)?", v) and not (len(v) > 1 and v[0] == "0" and "." not in v)
            key = nxt is not None and nxt.t == ":" and prev is not None and prev.t in ("{", ",")
            if plain and not key and not (prev and prev.t == "."):
                if "." in v:
                    new = repr(float(v) + 1)
                    add(t.line, f"`{v}` -> `{new}`", [(t.a, t.b, new)])
                else:
                    for d in (1, -1):
                        new = str(int(v) + d)
                        add(t.line, f"`{v}` -> `{new}`", [(t.a, t.b, f"({new})" if d < 0 and int(v) + d < 0 else new)])
        elif t.k == "id":
            if t.t in ("true", "false") and not (prev and prev.t == "."):
                add(t.line, f"`{t.t}` -> `{'false' if t.t == 'true' else 'true'}`",
                    [(t.a, t.b, "false" if t.t == "true" else "true")])
            elif t.t == "if" and nxt and nxt.t == "(" and (i + 1) in match and match[i + 1] > i + 2:
                o, c = nxt, toks[match[i + 1]]
                add(t.line, "negate `if` condition", [(o.b, o.b, "!("), (c.a, c.a, ")")])
            elif t.t == "return" and nxt and not nxt.nl and not (nxt.k == "p" and nxt.t in (";", "}")):
                e = _expr_end(toks, i + 1)
                edit = body_edit(i + 1, e) if e > i + 1 else None
                if edit:
                    add(t.line, "`return <value>` -> `return undefined`", [edit])
            if t.t not in KEYWORDS and _stmt_start(toks, match, i):
                cs = _call_stmt(toks, match, i)
                if cs and cs[0].split(".")[0] not in KEYWORDS and not NOISE_CALL.match(cs[0]):
                    end_tok = toks[cs[1]]
                    follows = toks[cs[1] + 1] if cs[1] + 1 < len(toks) else None
                    semi = follows is not None and follows.k == "p" and follows.t == ";"
                    add(t.line, "remove call statement", [(t.a, end_tok.b, "" if semi else ";")])
    seen, uniq = set(), []
    for cnd in cands:
        if cnd not in seen:
            seen.add(cnd)
            uniq.append(cnd)
    uniq.sort(key=lambda c: c[0])
    counts, nth, sites = {}, {}, []
    for line, label, _ in uniq:
        counts[(line, label)] = counts.get((line, label), 0) + 1
    for line, label, edits in uniq:
        nth[(line, label)] = nth.get((line, label), 0) + 1
        shown = f"{label} (#{nth[(line, label)]})" if counts[(line, label)] > 1 else label
        sites.append({"line": line, "label": shown, "kind": "edits", "edits": list(edits)})
    return sites


def js_apply(src: str, site) -> str:
    out = src
    for a, b, text in sorted(site["edits"], reverse=True):
        out = out[:a] + text + out[b:]
    return out


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


_ACTIVE: set = set()
_ACTIVE_LOCK = threading.Lock()


def kill_active() -> None:
    """Kill every test process still running (Ctrl+C / console close while mutants run in parallel)."""
    with _ACTIVE_LOCK:
        procs = list(_ACTIVE)
    for p in procs:
        kill_tree(p)


def run_cmd(cmd: str, cwd: Path, timeout: float):
    """Return (returncode or None on timeout, combined output, timed_out)."""
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1", CI="1", NO_COLOR="1", FORCE_COLOR="0",
               PYTHONIOENCODING="utf-8")
    kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" \
        else {"start_new_session": True}
    p = subprocess.Popen(cmd, shell=True, cwd=str(cwd), stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, **kw)
    with _ACTIVE_LOCK:
        _ACTIVE.add(p)
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
    finally:
        with _ACTIVE_LOCK:
            _ACTIVE.discard(p)


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

BACKUP_PREFIX = "honest-tests-backup-"


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        k32.GetExitCodeProcess(h, ctypes.byref(code))
        k32.CloseHandle(h)
        return code.value == 259  # STILL_ACTIVE
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Guard:
    """Snapshot files, write mutants, and always put the originals back (bytes and mtime).

    Every original is also written to a backup folder with a manifest, so that even a SIGKILL or a
    power cut can be undone with `mutation_check.py --restore`."""

    def __init__(self):
        self.orig: dict = {}
        self.tmp = Path(tempfile.mkdtemp(prefix=BACKUP_PREFIX))
        self.n = 0
        self.names: dict = {}
        self.manifest: dict = {"pid": os.getpid(), "files": {}}
        atexit.register(self.restore_all)

    def snapshot(self, path: Path) -> bytes:
        st = path.stat()
        data = path.read_bytes()
        self.orig[path] = (data, st.st_atime_ns, st.st_mtime_ns)
        self.n += 1
        name = f"{self.n}.bak"
        self.names[path] = name
        (self.tmp / name).write_bytes(data)
        self.manifest["files"][name] = {"path": str(path), "atime_ns": st.st_atime_ns,
                                        "mtime_ns": st.st_mtime_ns}
        (self.tmp / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        return data

    def expect(self, path: Path, mutant_mtime_ns: int) -> None:
        """Record, before a mutant is written, the mtime it will carry: after a SIGKILL this is how the
        next run can tell a mutant of ours from a file the user has edited since."""
        self.manifest["files"][self.names[path]]["mutant_mtime_ns"] = mutant_mtime_ns
        (self.tmp / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def write(self, path: Path, text: str, bump: int) -> None:
        mtime = self.orig[path][2]
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


# --------------------------------------------------------------------------- parallel workers, TS check

_COPY_SKIP = {".git", "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".cache", ".tox"}
_LINKED = {"node_modules", ".venv", "venv"}
TS_CHECK_JS = (
    "const ts=require(process.argv[1]);const fs=require('fs');const f=process.argv[2];"
    "const r=ts.transpileModule(fs.readFileSync(f,'utf8'),{fileName:f,reportDiagnostics:true,"
    "compilerOptions:{jsx:ts.JsxEmit.Preserve}});"
    "process.exit((r.diagnostics||[]).some(d=>d.category===ts.DiagnosticCategory.Error)?1:0)")


TS_RANGES_JS = r"""
const ts=require(process.argv[1]);const fs=require('fs');const f=process.argv[2];
const text=fs.readFileSync(f,'utf8');
const sf=ts.createSourceFile(f,text,ts.ScriptTarget.Latest,true,/\.[cm]?tsx$|\.jsx$/.test(f)?ts.ScriptKind.TSX:undefined);
const out=[];const add=(a,b)=>{if(b>a)out.push([a,b])};
const angle=a=>{if(!a||!a.length)return;const lt=text.lastIndexOf('<',a.pos);const gt=text.indexOf('>',a.end);
  if(lt>=0)add(lt,lt+1);if(gt>=0)add(gt,gt+1)};
const gaps=(n,keeps)=>{let p=n.getStart(sf);keeps.sort((x,y)=>x.pos-y.pos);
  for(const k of keeps){add(p,k.getStart(sf));p=k.end}add(p,n.end)};
const jsxExprs=n=>{const k=[];(function c(x){if(ts.isJsxExpression(x)){k.push(x);return}ts.forEachChild(x,c)})(n);return k};
function visit(n){
  if(ts.isInterfaceDeclaration(n)||ts.isTypeAliasDeclaration(n)||ts.isImportDeclaration(n)||
     ts.isExportDeclaration(n)||ts.isImportEqualsDeclaration(n)||
     ((ts.isFunctionDeclaration(n)||ts.isMethodDeclaration(n)||ts.isConstructorDeclaration(n))&&!n.body)||
     (n.modifiers&&n.modifiers.some(m=>m.kind===ts.SyntaxKind.DeclareKeyword))){add(n.getStart(sf),n.end);return}
  if(ts.isTypeNode(n)&&n.kind!==ts.SyntaxKind.ExpressionWithTypeArguments)add(n.getStart(sf),n.end);
  angle(n.typeArguments);angle(n.typeParameters);
  if(ts.isJsxText(n))add(n.getStart(sf),n.end);
  else if(ts.isJsxOpeningElement(n)||ts.isJsxSelfClosingElement(n))gaps(n,jsxExprs(n));
  else if(ts.isJsxClosingElement(n)||ts.isJsxOpeningFragment(n)||ts.isJsxClosingFragment(n))add(n.getStart(sf),n.end);
  ts.forEachChild(n,visit)}
visit(sf);
process.stdout.write(JSON.stringify(out));
"""


def ts_exclusions(node: str, tsjs: str, path: Path, src: str):
    """Character ranges that are types, declarations or JSX markup, according to the real TypeScript
    parser. None when it cannot be asked (then the heuristics in js_sites apply)."""
    try:
        r = subprocess.run([node, "-e", TS_RANGES_JS, tsjs, str(path)], capture_output=True, timeout=60)
        if r.returncode != 0:
            return None
        ranges = json.loads(r.stdout.decode("utf-8"))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None
    if any(ord(ch) > 0xFFFF for ch in src):  # TypeScript counts UTF-16 units, Python counts code points
        cp, n16 = [], 0
        for ch in src:
            cp.append(n16)
            n16 += 2 if ord(ch) > 0xFFFF else 1
        cp.append(n16)
        back = {u: i for i, u in enumerate(cp)}
        ranges = [[back.get(a, a), back.get(b, b)] for a, b in ranges]
    return [(a, b) for a, b in ranges]


TS_SERVER_JS = r"""
const ts=require(process.argv[1]),fs=require('fs'),path=require('path');
const rl=require('readline').createInterface({input:process.stdin});
const norm=f=>f.replace(/\\/g,'/');
const dir=norm(process.argv[2]);
const cfgPath=ts.findConfigFile(dir,ts.sys.fileExists,'tsconfig.json');
let options={noEmit:true,skipLibCheck:true},files=[];
if(cfgPath){const c=ts.readConfigFile(cfgPath,ts.sys.readFile);
  const p=ts.parseJsonConfigFileContent(c.config,ts.sys,path.dirname(cfgPath));
  options=Object.assign({},p.options,{noEmit:true,skipLibCheck:true,incremental:false,composite:false,
    declaration:false,declarationMap:false,emitDeclarationOnly:false});
  delete options.tsBuildInfoFile;files=p.fileNames.map(norm)}
const over=new Map(),ver=new Map();
const host={getScriptFileNames:()=>files,getScriptVersion:f=>String(ver.get(norm(f))||0),
  getScriptSnapshot:f=>{f=norm(f);const t=over.has(f)?over.get(f):(ts.sys.fileExists(f)?ts.sys.readFile(f):undefined);
    return t===undefined?undefined:ts.ScriptSnapshot.fromString(t)},
  getCurrentDirectory:()=>dir,getCompilationSettings:()=>options,getDefaultLibFileName:o=>ts.getDefaultLibFilePath(o),
  fileExists:f=>over.has(norm(f))||ts.sys.fileExists(f),readFile:f=>over.has(norm(f))?over.get(norm(f)):ts.sys.readFile(f),
  readDirectory:ts.sys.readDirectory,directoryExists:ts.sys.directoryExists,getDirectories:ts.sys.getDirectories};
const ls=ts.createLanguageService(host,ts.createDocumentRegistry());
let importers=null;
function build(){importers=new Map();const prog=ls.getProgram();
  for(const sf of prog.getSourceFiles()){if(sf.isDeclarationFile||prog.isSourceFileFromExternalLibrary(sf))continue;
    for(const imp of ts.preProcessFile(sf.text,true,true).importedFiles){
      const r=ts.resolveModuleName(imp.fileName,sf.fileName,options,host).resolvedModule;
      if(r){const k=norm(r.resolvedFileName);if(!importers.has(k))importers.set(k,[]);importers.get(k).push(norm(sf.fileName))}}}}
function keys(f){if(!importers)build();const out=[];
  for(const g of [f,...(importers.get(f)||[])])
    for(const d of ls.getSyntacticDiagnostics(g).concat(ls.getSemanticDiagnostics(g)))
      out.push(path.basename(g)+':'+d.code+':'+ts.flattenDiagnosticMessageText(d.messageText,' '));
  return out}
const send=o=>process.stdout.write(JSON.stringify(o)+'\n');
rl.on('line',line=>{let m;try{m=JSON.parse(line)}catch(e){return}
  try{
    if(m.op==='info'){send({id:m.id,version:ts.version,config:cfgPath||null});return}
    const f=norm(m.file);if(!files.includes(f))files.push(f);
    if(m.text!==undefined){over.set(f,m.text);ver.set(f,(ver.get(f)||0)+1)}
    let k;try{k=keys(f)}finally{if(m.text!==undefined){over.delete(f);ver.set(f,(ver.get(f)||0)+1)}}
    send({id:m.id,keys:k})
  }catch(e){send({id:m.id,error:String(e&&e.stack||e)})}});
"""


class TsChecker:
    """A long-lived TypeScript language service. A mutant that parses but no longer type-checks is
    not a bug the tests could catch: it is a compile error, so it is dropped (like Stryker's checker).
    Only errors the mutant ADDS count; the file itself and the files that import it are checked."""

    def __init__(self, node: str, tsjs: str, project: Path):
        self.p = subprocess.Popen([node, "-e", TS_SERVER_JS, tsjs, str(project)], stdin=subprocess.PIPE,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        self.lock = threading.Lock()
        self.n = 0
        self.base: dict = {}
        info = self._call({"op": "info"})
        self.version, self.config = info["version"], info["config"]

    def _call(self, msg: dict) -> dict:
        with self.lock:
            self.n += 1
            msg["id"] = self.n
            self.p.stdin.write((json.dumps(msg) + "\n").encode("utf-8"))
            self.p.stdin.flush()
            line = self.p.stdout.readline()
            if not line:
                raise RuntimeError("the TypeScript checker exited")
            r = json.loads(line)
            if "error" in r:
                raise RuntimeError(r["error"].splitlines()[0])
            return r

    def new_errors(self, path: Path, text: str) -> list:
        f = str(path)
        if f not in self.base:
            self.base[f] = set(self._call({"op": "check", "file": f})["keys"])
        return sorted(set(self._call({"op": "check", "file": f, "text": text})["keys"]) - self.base[f])

    def close(self) -> None:
        try:
            self.p.stdin.close()
            self.p.wait(5)
        except Exception:
            self.p.kill()
        finally:
            try:
                self.p.stdout.close()
            except Exception:
                pass


_STRIP_JS = ("const m=require('node:module'),fs=require('fs');"
             "process.stdout.write(m.stripTypeScriptTypes(fs.readFileSync(process.argv[1],'utf8'),{mode:'strip'}))")


def node_strip_types(node: str, path: Path, src: str):
    """Node's built-in TypeScript stripper (22.13+/23.2+): the source with every type erased to blanks, same
    length. None if unavailable, if it rejects the file (enums, JSX, ...) or if lengths would not line up."""
    try:
        r = subprocess.run([node, "--no-warnings", "-e", _STRIP_JS, str(path)], capture_output=True, timeout=60)
        if r.returncode != 0:
            return None
        out = r.stdout.decode("utf-8")
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError):
        return None
    return out if len(out) == len(src) else None


def node_strip_ok(node: str, text_file: Path) -> bool:
    """Does Node accept this (mutated) TypeScript file? Syntax only, no types."""
    try:
        return subprocess.run([node, "--no-warnings", "-e", _STRIP_JS, str(text_file)],
                              capture_output=True, timeout=60).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def find_typescript(project: Path) -> str | None:
    """The project's own TypeScript compiler, if installed (HONEST_TESTS_TYPESCRIPT overrides the search)."""
    env = os.environ.get("HONEST_TESTS_TYPESCRIPT")
    if env and Path(env).is_file():
        return env
    for d in [project, *project.parents]:
        cand = d / "node_modules" / "typescript" / "lib" / "typescript.js"
        if cand.is_file():
            return str(cand)
    return None


def make_workdir(project: Path, root: Path, name: str) -> Path:
    """A private copy of the project to run mutants in (big, reproducible dirs are linked, not copied)."""
    dst = root / name
    skip = _COPY_SKIP | _LINKED
    shutil.copytree(project, dst, ignore=lambda d, names: [n for n in names if n in skip])
    for n in _LINKED:
        src = project / n
        if src.is_dir():
            link = dst / n
            try:
                os.symlink(src, link, target_is_directory=True)
            except OSError:
                if os.name == "nt":
                    r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(src)],
                                       capture_output=True)
                    if r.returncode != 0:
                        shutil.copytree(src, link)
                else:
                    shutil.copytree(src, link)
    return dst


def remove_workdir(path: Path) -> None:
    for n in _LINKED:
        link = path / n
        if os.path.lexists(link):
            try:
                os.unlink(link)
            except OSError:
                try:
                    os.rmdir(link)  # Windows junction
                except OSError:
                    pass
    shutil.rmtree(path, ignore_errors=True)


_GUARD = None
_console_cb = None
_IO_LOCK = threading.Lock()   # mutant writes vs. the emergency restore
_ABORT = threading.Event()    # set once the console is being closed: never write another mutant


def _on_console_event(event: int) -> bool:
    """Windows closing the console window / logging off / shutting down: put the files back now."""
    if event in (2, 5, 6):
        with _IO_LOCK:
            _ABORT.set()
            if _GUARD is not None:
                _GUARD.restore_all()
        kill_active()
        return True
    return False


def install_console_close_handler() -> None:
    global _console_cb
    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes
        proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        _console_cb = proto(lambda ev: bool(_on_console_event(ev)) and False)  # restore, then let it end
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_console_cb, True)
    except Exception:
        pass


def stale_backups(project: Path | None = None):
    """Backup folders left by runs that died without restoring (process no longer alive)."""
    for d in Path(tempfile.gettempdir()).glob(BACKUP_PREFIX + "*"):
        try:
            man = json.loads((d / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if man.get("pid") == os.getpid() or pid_alive(int(man.get("pid", 0))):
            continue
        if project is not None and not any(
                str(project).lower() in e["path"].lower() for e in man["files"].values()):
            continue
        yield d, man


def restore_stale(project: Path | None = None) -> int:
    """Put back files from crashed runs. Returns the number of files that had been left mutated."""
    fixed = 0
    for d, man in list(stale_backups(project)):
        for name, e in man["files"].items():
            target, bak = Path(e["path"]), d / name
            try:
                data = bak.read_bytes()
                if not target.exists() or target.read_bytes() != data:
                    target.write_bytes(data)
                    fixed += 1
                    print(f"restored {target}")
                os.utime(target, ns=(e["atime_ns"], e["mtime_ns"]))
            except OSError as ex:
                print(f"could not restore {target}: {ex}", file=sys.stderr)
        shutil.rmtree(d, ignore_errors=True)
    return fixed


def heal_stale(project: Path):
    """After a hard kill (SIGKILL, power cut) put back files that are still our untouched mutants.
    A file whose mtime is not the one we gave the mutant was edited since: it is left alone and reported.
    Returns (files restored, backup folders that need a human decision)."""
    fixed, refused = 0, []
    for d, man in list(stale_backups(project)):
        plan, safe = [], True
        for name, e in man["files"].items():
            target = Path(e["path"])
            try:
                data = (d / name).read_bytes()
                if target.exists() and target.read_bytes() == data:
                    continue
                cur = target.stat().st_mtime_ns if target.exists() else None
                ours = cur is not None and "mutant_mtime_ns" in e and abs(cur - e["mutant_mtime_ns"]) <= 1000  # exact up to filesystem rounding
                if ours or cur is None:
                    plan.append((target, data, e))
                else:
                    safe = False
            except OSError:
                safe = False
        if not safe:
            refused.append(d)
            continue
        for target, data, e in plan:
            target.write_bytes(data)
            os.utime(target, ns=(e["atime_ns"], e["mtime_ns"]))
            fixed += 1
        shutil.rmtree(d, ignore_errors=True)
    return fixed, refused


# --------------------------------------------------------------------------- equivalent mutants

def _code_sig(code):
    consts = tuple(_code_sig(c) if hasattr(c, "co_code") else (type(c).__name__, repr(c))
                   for c in code.co_consts)
    return (code.co_code, consts, code.co_names, code.co_varnames, code.co_argcount)


def code_signature(src: str):
    return _code_sig(compile(src, "<sig>", "exec"))


def norm_source(line: str, lang: str) -> str:
    """Source line without its trailing comment, so ignore entries survive comment edits."""
    s = line.strip()
    return re.split(r"\s+#\s" if lang == "py" else r"\s+//\s", s + " ")[0].strip()


IGNORE_LINE = re.compile(r"^\s*(?P<file>[^|]+?)\s*\|\s*(?P<label>[^|]+?)\s*\|\s*(?P<src>.*?)\s+#\s+(?P<why>\S.*)$")


def load_ignores(path: Path):
    """Parse `file | mutation | source  # reason`. Entries without a reason are rejected."""
    entries, problems = [], []
    if not path.is_file():
        return entries, problems
    for n, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        m = IGNORE_LINE.match(raw)
        if not m:
            problems.append(f"{path.name}:{n}: needs `file | mutation | source  # reason` "
                            f"(a reason is required): {raw.strip()[:60]}")
            continue
        entries.append({"file": m["file"].replace("\\", "/"), "label": re.sub(r" \(#\d+\)$", "", m["label"]),
                        "src": m["src"], "why": m["why"], "used": False})
    return entries, problems


def find_ignore(entries, s):
    label = re.sub(r" \(#\d+\)$", "", s["label"])
    src = norm_source(s["source"], s["lang"])
    for e in entries:
        if e["file"] == s["file"] and e["label"] == label and e["src"] == src:
            e["used"] = True
            return e
    return None


# --------------------------------------------------------------------------- main

def trunc(s: str, n: int = 78) -> str:
    s = s.strip()
    return s if len(s) <= n else s[: n - 3] + "..."


def make_color(mode: str, enabled_for_json: bool):
    on = (mode == "always" or (mode == "auto" and sys.stdout.isatty() and not os.environ.get("NO_COLOR"))) \
        and not enabled_for_json
    if on and os.name == "nt":
        os.system("")  # switches the Windows console to ANSI mode

    def c(code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if on else text
    return c


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
    ap.add_argument("--jobs", type=int, default=1, metavar="N",
                    help="test mutants in N parallel copies of the project (default 1; 0 = auto). "
                         "Your files are never touched when N > 1. Needs a suite that can run twice at once.")
    ap.add_argument("--no-typecheck", action="store_true",
                    help="do not drop TypeScript mutants that stop type-checking")
    ap.add_argument("--seed", type=int, default=0, help="sampling seed (default 0)")
    ap.add_argument("--include", action="append", default=[], help="only files matching this glob")
    ap.add_argument("--exclude", action="append", default=[], help="skip files matching this glob")
    ap.add_argument("--ignore-file", help="accepted equivalent mutants (default <project>/.mutation-ignore)")
    ap.add_argument("--print-ignore", action="store_true",
                    help="print a ready-to-fill .mutation-ignore line for every survivor")
    ap.add_argument("--show", type=int, default=30, metavar="N",
                    help="list at most N survivors (default 30, 0 = all)")
    ap.add_argument("--restore", action="store_true",
                    help="undo a run that was killed (SIGKILL, power loss) and exit")
    ap.add_argument("--color", choices=["auto", "always", "never"], default="auto")
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
    c = make_color(args.color, args.json)

    if args.restore:
        n = restore_stale()
        print(f"{n} file(s) restored." if n else "Nothing to restore.")
        return 0

    project = Path(args.project).resolve()
    if not project.is_dir():
        print(f"error: --project {project} is not a directory", file=sys.stderr)
        return 2
    healed, refused = heal_stale(project)
    if healed:
        print(f"recovered {healed} file(s) left mutated by a run that was killed; originals put back.",
              file=sys.stderr)
    if refused:
        print("error: a previous run was killed while a mutant was on disk, and a file changed since, so "
              "I will not overwrite it on my own.\nCompare with the backup in "
              + ", ".join(str(d) for d in refused) + " or run `mutation_check.py --restore` to force it.",
              file=sys.stderr)
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
    log(c("1", "honest-tests / mutation-check"))
    log(f"  project : {'.' if project == Path.cwd().resolve() else project}")
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

    ignore_path = Path(args.ignore_file) if args.ignore_file else project / ".mutation-ignore"
    ignores, problems = load_ignores(ignore_path)
    for p in problems:
        log(c("33", f"  warning : {p}"))

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
    log(f"  baseline: {c('32', 'PASS')}" + (f", {n_tests} tests" if n_tests is not None else "")
        + f" in {base_secs:.1f}s (mutant timeout {timeout:.0f}s)")

    # Enumerate mutants.
    ts_node = shutil.which("node")
    ts_js = find_typescript(project) if ts_node else None
    tiers = set()
    mutants = []
    skipped_files = []
    for rel, path, lang, lines in targets:
        try:
            src = path.read_bytes().decode("utf-8")
            if lang == "py":
                sites = py_sites(src, lines)
            else:
                is_ts = path.suffix.lower() in TS_EXT
                excl = blank = None
                if ts_node and ts_js:
                    excl = ts_exclusions(ts_node, ts_js, path, src)
                if excl is None and is_ts and ts_node and path.suffix.lower() in (".ts", ".mts", ".cts"):
                    blank = node_strip_types(ts_node, path, src)
                    if blank is not None:
                        tiers.add("node")
                elif excl is not None:
                    tiers.add("typescript")
                elif is_ts:
                    tiers.add("heuristic")
                sites = js_sites(src, lines, ts=is_ts, exclude=excl, blanked=blank)
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
    if tiers:
        names = {"typescript": "the project's TypeScript parser", "node": "Node's built-in type stripper",
                 "heuristic": "built-in heuristics (no TypeScript parser available)"}
        log("  ts parse: " + " + ".join(names[k] for k in sorted(tiers)))
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
    global _GUARD
    _GUARD = guard
    install_console_close_handler()
    workroots = []
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    if hasattr(signal, "SIGBREAK"):  # Ctrl+Break on Windows
        signal.signal(signal.SIGBREAK, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    node_check = False
    results = []
    checker = None
    status_by_i = {}
    reasons = {}
    interrupted = False
    try:
        parsed, sigs = {}, {}
        node = shutil.which("node")
        for rel, path, lang, _ in targets:
            guard.snapshot(path)
            if lang == "py":
                try:
                    parsed[path] = ast.parse(guard.orig[path][0].decode("utf-8"))
                    sigs[path] = code_signature(ast.unparse(parsed[path]) + "\n")
                except (SyntaxError, ValueError):
                    parsed[path] = None
        if node and any(s["lang"] == "js" and s["path"].suffix in (".js", ".cjs", ".mjs") for s in mutants):
            node_check = True
        started = time.time()
        has_ts = bool(node) and any(s["lang"] == "js" and s["path"].suffix.lower() in TS_EXT for s in mutants)
        tsjs = find_typescript(project) if has_ts else None
        strip_ok = False
        if has_ts and not tsjs:
            strip_ok = subprocess.run([node, "--no-warnings", "-e", "require('node:module').stripTypeScriptTypes('let a: number')"],
                                      capture_output=True).returncode == 0
        if has_ts and tsjs and not args.no_typecheck:
            try:
                checker = TsChecker(node, tsjs, project)
                log(f"  typecheck: TypeScript {checker.version}, "
                    + (f"tsconfig {Path(checker.config).name}" if checker.config else "no tsconfig (defaults)")
                    + "; mutants that stop type-checking are dropped")
            except (OSError, RuntimeError, KeyError, ValueError) as e:
                checker = None
                log(c("33", f"  typecheck: unavailable ({e}); checking syntax only"))
        todo = []
        for i, s in enumerate(mutants, 1):
            entry = find_ignore(ignores, s)
            if entry:
                status_by_i[i] = "accepted"
                reasons[id(s)] = entry["why"]
            else:
                todo.append((i, s))

        def evaluate(i, s, workdir):
            """Status of one mutant. With a workdir the mutant is written to the copy, never to the project."""
            path = s["path"]
            data, atime, mtime = guard.orig[path]
            original = data.decode("utf-8")
            if workdir is None:
                proj, target = project, path
            else:
                proj, target = workdir, workdir / path.relative_to(project)

            def put(text):
                with _IO_LOCK:
                    if _ABORT.is_set():
                        raise KeyboardInterrupt
                    if workdir is None:
                        guard.expect(path, mtime + i * 10**9)
                    target.write_bytes(text.encode("utf-8"))
                    os.utime(target, ns=(mtime + i * 10**9, mtime + i * 10**9))  # defeat .pyc/cache reuse

            status = "survived"
            try:
                if s["lang"] == "py":
                    mutated = py_apply(copy.deepcopy(parsed[path]), s)
                    try:
                        compile(mutated, str(path), "exec")
                        if path in sigs and code_signature(mutated) == sigs[path]:
                            status = "equivalent"  # identical bytecode: provably cannot change behaviour
                    except (SyntaxError, ValueError):
                        status = "invalid"
                else:
                    mutated = js_apply(original, s)
                    if checker is not None and path.suffix.lower() in TS_EXT:
                        try:
                            if checker.new_errors(path, mutated):
                                status = "invalid"  # compiles to nothing the tests could run
                        except RuntimeError:
                            pass
                if status == "survived":
                    put(mutated)
                    suffix = path.suffix.lower()
                    if s["lang"] == "js" and node_check and suffix in (".js", ".cjs", ".mjs"):
                        if subprocess.run([node, "--check", str(target)], capture_output=True).returncode != 0:
                            status = "invalid"
                    elif s["lang"] == "js" and tsjs and checker is None and suffix in TS_EXT:
                        if subprocess.run([node, "-e", TS_CHECK_JS, tsjs, str(target)],
                                          capture_output=True).returncode != 0:
                            status = "invalid"
                    elif s["lang"] == "js" and strip_ok and suffix in (".ts", ".mts", ".cts"):
                        if not node_strip_ok(node, target):
                            status = "invalid"
                    if status == "survived":
                        code, out, timed_out = run_cmd(cmd, proj, timeout)
                        if timed_out:
                            status = "timeout"
                        elif code == 0:
                            status = "survived"
                        elif s["lang"] == "js" and INVALID_JS.search(out):
                            status = "invalid"
                        else:
                            status = "killed"
            finally:
                with _IO_LOCK:
                    if not _ABORT.is_set():  # after a console close the emergency restore has the last word
                        target.write_bytes(data)
                        os.utime(target, ns=(atime, mtime))
            return status

        def tick():
            if sys.stderr.isatty() and not args.json:
                sys.stderr.write(f"\r  testing mutant {len(status_by_i)}/{len(mutants)} "
                                 f"({time.time() - started:.0f}s)   ")
                sys.stderr.flush()

        jobs = args.jobs if args.jobs > 0 else min(4, os.cpu_count() or 1)
        jobs = max(1, min(jobs, len(todo)))
        if jobs > 1:
            try:
                workroot = Path(tempfile.mkdtemp(prefix="honest-tests-work-"))
                workroots.append(workroot)
                log(f"  parallel: {jobs} workers (copies of the project; your files are not touched)")

                def prepare(k):
                    wd = make_workdir(project, workroot, f"w{k}")
                    code, out, timed_out = run_cmd(cmd, wd, max(timeout, base_secs * 5))
                    if code != 0:
                        raise RuntimeError(f"the suite does not pass in a copy of the project (worker {k})")
                    for _, path, lang, _ in targets:  # canary: break every target, the suite must notice
                        (wd / path.relative_to(project)).write_text(
                            "raise RuntimeError('honest-tests canary')\n" if lang == "py"
                            else "throw new Error('honest-tests canary');\n", encoding="utf-8")
                    code, out, timed_out = run_cmd(cmd, wd, max(timeout, base_secs * 5))
                    if code == 0 and not timed_out:
                        raise RuntimeError("the suite does not run the copied sources (installed or "
                                           "absolute-path imports); mutants would be invisible to it")
                    for _, path, _, _ in targets:
                        dst = wd / path.relative_to(project)
                        dst.write_bytes(guard.orig[path][0])
                        os.utime(dst, ns=guard.orig[path][1:])
                    return wd

                with ThreadPoolExecutor(jobs) as ex:
                    workdirs = list(ex.map(prepare, range(jobs)))
            except (RuntimeError, OSError, ValueError) as e:
                log(c("33", f"  parallel disabled: {e}; running one mutant at a time"))
                jobs, workdirs = 1, []

        if jobs > 1:
            free = queue.Queue()
            for wd in workdirs:
                free.put(wd)
            stop = threading.Event()

            def task(i, s):
                wd = free.get()
                try:
                    if stop.is_set():
                        return None
                    st = evaluate(i, s, wd)
                    return None if stop.is_set() else st
                finally:
                    free.put(wd)

            ex = ThreadPoolExecutor(jobs)
            futs = {}
            try:
                futs = {ex.submit(task, i, s): i for i, s in todo}
                for f in as_completed(futs):
                    st = f.result()
                    if st is not None:
                        status_by_i[futs[f]] = st
                        tick()
            except BaseException:  # Ctrl+C, SIGTERM, console close: stop quickly
                stop.set()
                kill_active()
                for f in futs:
                    f.cancel()
                raise
            finally:
                ex.shutdown(wait=True)
        else:
            for i, s in todo:
                status_by_i[i] = evaluate(i, s, None)
                tick()
    except KeyboardInterrupt:
        interrupted = True
    finally:
        restored = guard.restore_all()
        if checker is not None:
            checker.close()
        for wr in workroots:
            for sub_ in wr.iterdir() if wr.exists() else []:
                remove_workdir(sub_)
            shutil.rmtree(wr, ignore_errors=True)
        results = [(s, status_by_i[i]) for i, s in enumerate(mutants, 1) if i in status_by_i]
        if sys.stderr.isatty() and not args.json:
            sys.stderr.write("\r" + " " * 60 + "\r")

    survived = [(s, st) for s, st in results if st == "survived"]
    killed = [(s, st) for s, st in results if st in ("killed", "timeout")]
    invalid = [(s, st) for s, st in results if st == "invalid"]
    equivalent = [(s, st) for s, st in results if st == "equivalent"]
    accepted = [(s, st) for s, st in results if st == "accepted"]
    valid = len(survived) + len(killed)
    score = round(100 * len(killed) / valid) if valid else None
    stale = [e for e in ignores if not e["used"]]

    if args.json:
        print(json.dumps({
            "base": base_desc, "command": cmd, "baseline_tests": n_tests, "restored": restored,
            "interrupted": interrupted, "score": score, "killed": len(killed),
            "survived": len(survived), "invalid": len(invalid), "equivalent": len(equivalent),
            "accepted": len(accepted), "min_score": args.min_score,
            "mutants": [{"file": s["file"], "line": s["line"], "mutation": s["label"],
                         "source": s["source"].strip(), "status": st,
                         **({"reason": reasons[id(s)]} if id(s) in reasons else {})}
                        for s, st in results],
        }, indent=2))
    else:
        cols = shutil.get_terminal_size((110, 24)).columns

        def row(s, extra=""):
            head = f"  {s['file']}:{s['line']:<4} {s['label']:<34} | "
            return head + trunc(s["source"], max(20, min(70, cols - len(head) - len(extra) - 1))) + extra
        if survived:
            shown = survived if args.show == 0 else survived[: args.show]
            print(c("31;1", f"\nSURVIVED ({len(survived)})") + ": every test still passed with this bug in place")
            for s, _ in shown:
                print(c("31", row(s)))
            if len(shown) < len(survived):
                print(f"  ... and {len(survived) - len(shown)} more (--show 0 lists all)")
        if args.verbose and killed:
            print(c("32;1", f"\nKILLED ({len(killed)})"))
            for s, st in killed:
                print(row(s, "  [timeout]" if st == "timeout" else ""))
        if accepted:
            print(f"\nACCEPTED as equivalent ({len(accepted)}), excluded from the score; reasons in {ignore_path.name}:")
            for s, _ in accepted:
                print(row(s, f"  # {reasons[id(s)]}"))
        extras = []
        if equivalent:
            extras.append(f"{len(equivalent)} identical-bytecode (provably equivalent)")
        if invalid:
            extras.append(f"{len(invalid)} did not compile")
        if extras:
            print("\nNot counted: " + ", ".join(extras) + ".")
        for e in stale:
            print(c("33", f"warning: unused ignore entry (code changed?): {e['file']} | {e['label']} | {e['src'][:40]}"))
        print()
        tests = f"{n_tests} tests pass, " if n_tests is not None else ""
        if score is None:
            print("Mutation score: n/a (no valid mutants)")
        else:
            ok = score >= args.min_score
            verdict = c("32;1", "PASS") if ok else c("31;1", "FAIL")
            print(f"{tests}{len(survived)} of {valid} mutants survived. "
                  f"Mutation score: {score}% (min {args.min_score:g}%) -> {verdict}")
        print("Source files restored: " + ("yes, byte-identical" if restored else "NO - run --restore"))
        if survived and args.print_ignore:
            print("\nIf (and only if) a survivor cannot change behaviour, add its line to "
                  f"{ignore_path.name} with a real reason:")
            for s, _ in survived:
                print(f"{s['file']} | {re.sub(r' [(]#[0-9]+[)]$', '', s['label'])} | "
                      f"{norm_source(s['source'], s['lang'])}  # REASON")
        elif survived:
            print()
            print("Next: for each survivor, write a test that fails when that exact change is applied.")
            print("Truly equivalent? --print-ignore lists them; a recorded reason is required.")
    if interrupted:
        print("Interrupted. Files restored." if restored else "Interrupted.", file=sys.stderr)
        return 130
    if score is not None and score < args.min_score:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
