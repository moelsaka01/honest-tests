"""Equivalent-mutant handling and crash recovery, with real processes and real signals."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import _paths
from test_mutation_check import STRONG, WEAK, Repo, mc


class EquivalentMutantTests(unittest.TestCase):
    def test_identical_bytecode_is_detected(self):
        a = mc.code_signature("def f():\n    return 0 + 0\n")
        b = mc.code_signature("def f():\n    return 0 - 0\n")
        c = mc.code_signature("def f():\n    return 1 + 0\n")
        self.assertEqual(a, b)  # constant folding: provably the same program
        self.assertNotEqual(a, c)

    def test_equivalent_survivor_is_accepted_with_a_reason(self):
        repo = Repo(STRONG)
        try:
            first = json.loads(repo.run("--json").stdout)
            survivors = [m for m in first["mutants"] if m["status"] == "survived"]
            self.assertTrue(survivors)
            lines = [f"m.py | {m['mutation']} | {m['source']}  # returns lo either way" for m in survivors]
            (repo.dir / ".mutation-ignore").write_text("\n".join(lines) + "\n", encoding="utf-8")
            second = json.loads(repo.run("--json").stdout)
            self.assertEqual(second["survived"], 0)
            self.assertEqual(second["accepted"], len(survivors))
            self.assertEqual(second["score"], 100)
            self.assertTrue(all("reason" in m for m in second["mutants"] if m["status"] == "accepted"))
        finally:
            repo.close()

    def test_ignore_entry_without_a_reason_is_rejected(self):
        repo = Repo(WEAK)
        try:
            path = repo.dir / ".mutation-ignore"
            path.write_text("m.py | `<` -> `>=` | if x < lo:\n", encoding="utf-8")
            self.assertEqual(json.loads(repo.run("--json").stdout)["accepted"], 0)
            entries, problems = mc.load_ignores(path)
            self.assertEqual(entries, [])
            self.assertEqual(len(problems), 1)
        finally:
            repo.close()

    def test_print_ignore_emits_fillable_lines(self):
        repo = Repo(WEAK)
        try:
            out = repo.run("--print-ignore").stdout
            self.assertRegex(out, r"m\.py \| .+ \| .+  # REASON")
        finally:
            repo.close()

    def test_show_limits_the_survivor_list(self):
        repo = Repo(WEAK)
        try:
            out = repo.run("--show", "2").stdout
            self.assertRegex(out, r"\.\.\. and \d+ more")
        finally:
            repo.close()


SLOW_CMD = f'"{sys.executable}" -c "import time, m; time.sleep(1.5)"'


class CrashRecoveryTests(unittest.TestCase):
    """The working tree must come back after Ctrl+C, SIGTERM and even a hard kill."""

    def _start(self, d):
        kw = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {}
        return subprocess.Popen([sys.executable, str(_paths.MUTATION_CHECK), "--files", "m.py",
                                 "--cmd", SLOW_CMD, "--max-mutants", "6"], cwd=d,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)

    def _project(self):
        d = Path(tempfile.mkdtemp(prefix="ht-crash-"))
        (d / "m.py").write_text("def f(a, b):\n    return a + b\n", encoding="utf-8")
        return d, (d / "m.py").read_bytes()

    def _wait_for_mutant(self, d, original, proc, timeout=30):
        end = time.time() + timeout
        while time.time() < end and proc.poll() is None:
            if (d / "m.py").read_bytes() != original:
                return True
            time.sleep(0.05)
        return False

    def test_ctrl_c_restores(self):
        d, original = self._project()
        try:
            proc = self._start(d)
            self.assertTrue(self._wait_for_mutant(d, original, proc), "mutant never written")
            proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGINT)
            proc.communicate(timeout=60)
            self.assertEqual((d / "m.py").read_bytes(), original)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    @unittest.skipIf(os.name == "nt", "a child cannot receive SIGTERM on Windows")
    def test_sigterm_restores(self):
        d, original = self._project()
        try:
            proc = self._start(d)
            self.assertTrue(self._wait_for_mutant(d, original, proc), "mutant never written")
            proc.send_signal(signal.SIGTERM)
            proc.communicate(timeout=60)
            self.assertEqual((d / "m.py").read_bytes(), original)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    @unittest.skipUnless(os.name == "nt", "Windows only: closing the console is its termination request")
    def test_console_host_dying_mid_run_restores_the_files(self):
        """Windows has no SIGTERM from another process (os.kill maps it to TerminateProcess, which nothing can
        catch). The nearest real, external event that can be automated is the console going away under the run:
        end the console host that owns the child's console. The files must come back. (The CTRL_CLOSE handler
        body is covered separately by ConsoleCloseTests; this shows the whole tool survives its console dying.)"""
        d, original = self._project()
        proc = None
        try:
            proc = subprocess.Popen([sys.executable, str(_paths.MUTATION_CHECK), "--files", "m.py",
                                     "--cmd", SLOW_CMD, "--max-mutants", "6"], cwd=d,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=subprocess.CREATE_NEW_CONSOLE)
            self.assertTrue(self._wait_for_mutant(d, original, proc), "mutant never written")
            find = (f"Get-CimInstance Win32_Process | Where-Object {{ $_.ParentProcessId -eq {proc.pid} "
                    "-and $_.Name -match 'conhost|OpenConsole' } | ForEach-Object { $_.ProcessId }")
            host = subprocess.run(["powershell", "-NoProfile", "-Command", find],
                                  capture_output=True, text=True, timeout=60).stdout.split()
            if not host:
                self.skipTest("no private console host for the child (ConPTY / headless session)")
            # Ending the console host is what closing its window does: attached processes get CTRL_CLOSE_EVENT.
            subprocess.run(["taskkill", "/F", "/PID", host[0]], capture_output=True, timeout=60)
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                self.fail("process did not end after its console window was closed")
            self.assertEqual((d / "m.py").read_bytes(), original)
        finally:
            if proc is not None and proc.poll() is None:
                proc.kill()
            shutil.rmtree(d, ignore_errors=True)

    def test_sigterm_handler_restores_on_every_platform(self):
        """Windows cannot deliver SIGTERM from another process, but Python's handler is real there:
        raise it from inside the run, while a mutant is on disk, and check the file comes back."""
        d, original = self._project()
        harness = d / "harness.py"
        try:
            harness.write_text(
                "import signal, sys\n"
                f"sys.path.insert(0, {str(_paths.MUTATION_CHECK.parent)!r})\n"
                "import mutation_check as mc\n"
                "real, calls = mc.run_cmd, []\n"
                "def fake(cmd, cwd, timeout):\n"
                "    calls.append(1)\n"
                "    if len(calls) == 2:  # call 1 is the baseline, call 2 runs the first mutant\n"
                "        print('mutant on disk:', (cwd / 'm.py').read_bytes() != ORIGINAL, flush=True)\n"
                "        signal.raise_signal(signal.SIGTERM)\n"
                "    return real(cmd, cwd, timeout)\n"
                "mc.run_cmd = fake\n"
                f"ORIGINAL = {original!r}\n"
                f"sys.exit(mc.main(['--files', 'm.py', '--cmd', {SLOW_CMD!r}]))\n", encoding="utf-8")
            proc = subprocess.run([sys.executable, str(harness)], cwd=d, capture_output=True, text=True,
                                  timeout=120)
            self.assertEqual(proc.returncode, 143, proc.stdout + proc.stderr)
            self.assertIn("mutant on disk: True", proc.stdout)
            self.assertEqual((d / "m.py").read_bytes(), original)
            self.assertNotIn("WARNING", proc.stderr)  # restored cleanly, backups removed
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def _kill_mid_mutant(self, d, original):
        proc = self._start(d)
        self.assertTrue(self._wait_for_mutant(d, original, proc), "mutant never written")
        proc.kill()
        proc.communicate(timeout=60)
        self.assertNotEqual((d / "m.py").read_bytes(), original, "kill came too late to test")

    def test_next_run_heals_a_hard_kill_by_itself(self):
        d, original = self._project()
        try:
            self._kill_mid_mutant(d, original)
            again = subprocess.run([sys.executable, str(_paths.MUTATION_CHECK), "--files", "m.py", "--list",
                                    "--cmd", SLOW_CMD],
                                   cwd=d, capture_output=True, text=True)
            self.assertEqual(again.returncode, 0, again.stderr)
            self.assertIn("recovered 1 file(s)", again.stderr)
            self.assertEqual((d / "m.py").read_bytes(), original)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_a_file_edited_after_the_kill_is_never_overwritten(self):
        d, original = self._project()
        try:
            self._kill_mid_mutant(d, original)
            time.sleep(0.05)
            (d / "m.py").write_text("def f(a, b):\n    return a * b  # my edit\n", encoding="utf-8")
            blocked = subprocess.run([sys.executable, str(_paths.MUTATION_CHECK), "--files", "m.py",
                                      "--cmd", SLOW_CMD], cwd=d, capture_output=True, text=True)
            self.assertEqual(blocked.returncode, 2)
            self.assertIn("--restore", blocked.stderr)
            self.assertIn("my edit", (d / "m.py").read_text(encoding="utf-8"))
            forced = subprocess.run([sys.executable, str(_paths.MUTATION_CHECK), "--restore"], cwd=d,
                                    capture_output=True, text=True)
            self.assertIn("restored", forced.stdout)
            self.assertEqual((d / "m.py").read_bytes(), original)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
