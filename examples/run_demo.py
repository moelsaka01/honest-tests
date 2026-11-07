#!/usr/bin/env python3
"""30-second demo: a real git diff, weak tests, then strong tests, scored by mutation_check.py.

    python examples/run_demo.py            # Python and JavaScript demos
    python examples/run_demo.py python     # only the Python one
    python examples/run_demo.py js         # only the JavaScript one (needs Node 18+)

For each demo it builds a throwaway git repo whose HEAD holds the buggy "before" code, then writes
the bug-fixed "after" code plus a test suite into the working tree (what an agent leaves behind),
and runs `mutation_check.py` against HEAD. Nothing in this repository is modified.
"""
import hashlib
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
CHECK = HERE.parent / "skills" / "mutation-check" / "scripts" / "mutation_check.py"
PY = sys.executable if " " not in sys.executable else f'"{sys.executable}"'

DEMOS = {
    "python": {"dir": "python-demo", "src": "pricing.py", "tests": "test_pricing.py",
               "cmd": f"{PY} -m unittest discover -s tests", "needs": None},
    "js": {"dir": "js-demo", "src": "cart.js", "tests": "cart.test.js",
           "cmd": "node --test tests/cart.test.js", "needs": "node"},
}


def sh(cwd, *args):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)


def state(root):
    h = hashlib.sha256()
    for p in sorted(root.rglob("*")):
        if p.is_file() and ".git" not in p.parts and "__pycache__" not in p.parts:
            h.update(p.relative_to(root).as_posix().encode() + p.read_bytes())
    return h.hexdigest()


def run(name):
    d = DEMOS[name]
    if d["needs"] and not shutil.which(d["needs"]):
        print(f"[{name}] skipped: `{d['needs']}` not found on PATH")
        return 0
    src = HERE / d["dir"]
    status = 0
    with tempfile.TemporaryDirectory(prefix="honest-tests-demo-") as tmp:
        repo = Path(tmp)
        sh(repo, "git", "init", "-q")
        sh(repo, "git", "config", "user.email", "demo@example.com")
        sh(repo, "git", "config", "user.name", "demo")
        shutil.copy(src / "before" / d["src"], repo / d["src"])
        sh(repo, "git", "add", "-A")
        sh(repo, "git", "commit", "-qm", "before the fix")
        shutil.copy(src / d["src"], repo / d["src"])          # the agent's bug fix
        (repo / "tests").mkdir()
        for label, folder in (("WEAK TESTS (what the agent wrote)", "tests_weak"),
                              ("STRENGTHENED TESTS", "tests_strong")):
            shutil.copy(src / folder / d["tests"], repo / "tests" / d["tests"])
            print(f"\n{'=' * 78}\n[{name}] {label}\n{'=' * 78}", flush=True)
            before = state(repo)
            r = subprocess.run([sys.executable, str(CHECK), "--cmd", d["cmd"]], cwd=repo)
            same = state(repo) == before
            print(f"[{name}] working tree identical after the run: {'yes' if same else 'NO'}")
            if not same:
                status = 1
    return status


if __name__ == "__main__":
    wanted = sys.argv[1:] or list(DEMOS)
    rc = 0
    for n in wanted:
        if n not in DEMOS:
            sys.exit(f"unknown demo {n!r}; choose from {', '.join(DEMOS)}")
        rc |= run(n)
    sys.exit(rc)
