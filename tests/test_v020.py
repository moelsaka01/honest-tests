"""v0.2.0: parallel workers, Windows console close, TypeScript syntax check."""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import _paths
from test_mutation_check import CMD, STRONG, WEAK, Repo, mc

NODE = shutil.which("node")


class ParallelTests(unittest.TestCase):
    def test_parallel_matches_sequential_and_never_touches_the_project(self):
        for tests in (WEAK, STRONG):
            repo = Repo(tests)
            try:
                before = (repo.dir / "m.py").read_bytes()
                seq = json.loads(repo.run("--json", "--jobs", "1").stdout)
                par_run = repo.run("--json", "--jobs", "3")
                par = json.loads(par_run.stdout)
                self.assertEqual([m["status"] for m in seq["mutants"]], [m["status"] for m in par["mutants"]])
                self.assertEqual(seq["score"], par["score"])
                self.assertEqual((repo.dir / "m.py").read_bytes(), before)
                self.assertEqual(list(Path(tempfile.gettempdir()).glob("honest-tests-work-*")), [])
            finally:
                repo.close()

    def test_parallel_announces_itself(self):
        repo = Repo(WEAK)
        try:
            self.assertIn("parallel: 2 workers", repo.run("--jobs", "2").stdout)
        finally:
            repo.close()

    def test_suite_that_ignores_the_copy_falls_back_to_sequential(self):
        """Tests that import the original by absolute path would make every mutant look like a survivor."""
        repo = Repo(WEAK)
        try:
            (repo.dir / "tests" / "test_m.py").write_text(
                "import importlib.util, unittest\n"
                f"spec = importlib.util.spec_from_file_location('m', {str(repo.dir / 'm.py')!r})\n"
                "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
                "class T(unittest.TestCase):\n"
                "    def test_ok(self):\n        self.assertEqual(m.clamp(5, 0, 10), 5)\n", encoding="utf-8")
            out = repo.run("--jobs", "2")
            self.assertIn("parallel disabled", out.stdout)
            self.assertIn("Source files restored: yes", out.stdout)
        finally:
            repo.close()

    def test_suite_failing_in_the_copy_falls_back_to_sequential(self):
        repo = Repo(WEAK)
        try:
            (repo.dir / "tests" / "test_m.py").write_text(
                "import os, unittest, m\n"
                "class T(unittest.TestCase):\n    def test_ok(self):\n"
                f"        self.assertEqual(os.path.basename(os.getcwd()), {repo.dir.name!r})\n"
                "        self.assertIsNotNone(m.clamp(5, 0, 10))\n", encoding="utf-8")
            out = repo.run("--jobs", "2")
            self.assertIn("parallel disabled: the suite does not pass in a copy", out.stdout)
            self.assertIn("Mutation score", out.stdout)  # the run still completes, sequentially
        finally:
            repo.close()

    def test_interrupt_kills_workers_and_leaves_no_copies(self):
        d = Path(tempfile.mkdtemp(prefix="ht-par-"))
        try:
            (d / "m.py").write_text("def f(a, b):\n    return a + b\n", encoding="utf-8")
            slow = f'"{sys.executable}" -c "import time, m; time.sleep(2)"'
            kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
            proc = subprocess.Popen([sys.executable, str(_paths.MUTATION_CHECK), "--files", "m.py", "--cmd", slow,
                                     "--jobs", "2", "--max-mutants", "6"], cwd=d, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=True, **kw)
            end = time.time() + 40
            while time.time() < end and not list(Path(tempfile.gettempdir()).glob("honest-tests-work-*/w1")):
                time.sleep(0.2)
            time.sleep(6)  # let the workers get past their sanity runs
            import signal
            proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
            out, err = proc.communicate(timeout=60)
            self.assertEqual((d / "m.py").read_text(encoding="utf-8"), "def f(a, b):\n    return a + b\n")
            self.assertEqual(list(Path(tempfile.gettempdir()).glob("honest-tests-work-*")), [])
        finally:
            shutil.rmtree(d, ignore_errors=True)


class ConsoleCloseTests(unittest.TestCase):
    def test_console_close_event_restores_files_immediately(self):
        """The Windows handler body: CLOSE/LOGOFF/SHUTDOWN put the original files back at once."""
        d = Path(tempfile.mkdtemp(prefix="ht-close-"))
        try:
            original = "def f(a, b):\n    return a + b\n"
            (d / "m.py").write_text(original, encoding="utf-8")
            harness = d / "harness.py"
            harness.write_text(
                "import sys\n"
                f"sys.path.insert(0, {str(_paths.MUTATION_CHECK.parent)!r})\n"
                "import mutation_check as mc\n"
                "real, calls = mc.run_cmd, []\n"
                "def fake(cmd, cwd, timeout):\n"
                "    calls.append(1)\n"
                "    if len(calls) == 2:\n"
                "        print('mutant on disk:', (cwd / 'm.py').read_text() != ORIGINAL, flush=True)\n"
                "        print('handled:', mc._on_console_event(2), flush=True)\n"
                "        print('restored at once:', (cwd / 'm.py').read_text() == ORIGINAL, flush=True)\n"
                "    return real(cmd, cwd, timeout)\n"
                "mc.run_cmd = fake\n"
                f"ORIGINAL = {original!r}\n"
                f"sys.exit(mc.main(['--files', 'm.py', '--cmd', {sys.executable + ' -c pass'!r}]))\n",
                encoding="utf-8")
            p = subprocess.run([sys.executable, str(harness)], cwd=d, capture_output=True, text=True, timeout=120)
            for line in ("mutant on disk: True", "handled: True", "restored at once: True"):
                self.assertIn(line, p.stdout, p.stdout + p.stderr)
            self.assertEqual((d / "m.py").read_text(encoding="utf-8"), original)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_other_console_events_are_ignored(self):
        self.assertFalse(mc._on_console_event(0))  # CTRL_C is handled elsewhere


@unittest.skipUnless(NODE, "node not installed")
class TypeScriptCheckTests(unittest.TestCase):
    """Wiring test against a stand-in compiler: the real one is the project's own `typescript` package."""

    FAKE_TS = (
        "exports.JsxEmit = {Preserve: 1};\n"
        "exports.DiagnosticCategory = {Error: 1};\n"
        "exports.transpileModule = (src) => ({diagnostics: /@@broken@@/.test(src) ? [{category: 1}] : []});\n")

    def test_find_typescript_walks_up_and_check_script_runs(self):
        d = Path(tempfile.mkdtemp(prefix="ht-ts-"))
        try:
            lib = d / "node_modules" / "typescript" / "lib"
            lib.mkdir(parents=True)
            (lib / "typescript.js").write_text(self.FAKE_TS, encoding="utf-8")
            sub = d / "pkg"
            sub.mkdir()
            found = mc.find_typescript(sub)
            self.assertEqual(Path(found), lib / "typescript.js")
            good, bad = d / "good.ts", d / "bad.ts"
            good.write_text("const a: number = 1;\n", encoding="utf-8")
            bad.write_text("const a = @@broken@@;\n", encoding="utf-8")
            run = lambda f: subprocess.run([NODE, "-e", mc.TS_CHECK_JS, found, str(f)], capture_output=True).returncode
            self.assertEqual(run(good), 0)
            self.assertEqual(run(bad), 1)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
