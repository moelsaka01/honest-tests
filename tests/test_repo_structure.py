"""Every skill must be well-formed and every demo must tell the truth."""
import json
import re
import shutil
import subprocess
import sys
import unittest

import _paths

ROOT = _paths.ROOT
SKILLS = ROOT / "skills"
EXPECTED = {"mutation-check", "mock-audit", "assertion-strength", "done-means-verified", "test-roast"}


def frontmatter(text):
    text = text.replace("\r\n", "\n")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert m, "missing YAML front matter"
    out = {}
    for line in m.group(1).splitlines():
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip()
    return out


class SkillFileTests(unittest.TestCase):
    def test_exactly_the_five_skills_exist(self):
        self.assertEqual({p.name for p in SKILLS.iterdir() if p.is_dir()}, EXPECTED)

    def test_frontmatter_and_output_contract(self):
        for name in EXPECTED:
            text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
            fm = frontmatter(text)
            self.assertEqual(set(fm), {"name", "description"}, name)
            self.assertEqual(fm["name"], name)
            self.assertTrue(re.fullmatch(r"[a-z0-9-]{1,64}", fm["name"]))
            self.assertTrue(40 < len(fm["description"]) <= 1024, f"{name}: {len(fm['description'])}")
            self.assertIn("## Output contract", text, name)
            self.assertIn("## Works with", text, name)

    def test_scripts_named_in_skills_exist_and_readme_documents_them(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for name in EXPECTED:
            text = (SKILLS / name / "SKILL.md").read_text(encoding="utf-8")
            for ref in re.findall(r"<skill-dir>/((?:\.\./)?[\w./-]+\.py)", text):
                self.assertTrue((SKILLS / name / ref).resolve().is_file(), f"{name}: {ref}")
        for script in ("mutation_check.py", "smell_scan.py"):
            self.assertIn(script, readme)
        for name in EXPECTED:
            self.assertIn(f"skills/{name}/SKILL.md", readme, name)

    def test_readme_relative_links_resolve(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for target in re.findall(r"\]\(((?!http|#)[^)\s]+)\)", readme):
            if target == "assets/demo.gif":
                continue  # placeholder until the GIF is recorded
            self.assertTrue((ROOT / target.split("#")[0]).exists(), target)

    def test_plugin_manifests(self):
        plugin = json.loads((ROOT / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text(encoding="utf-8"))
        self.assertEqual(plugin["name"], "honest-tests")
        self.assertEqual(market["plugins"][0]["name"], "honest-tests")
        self.assertTrue((ROOT / "LICENSE").is_file())


class DemoTests(unittest.TestCase):
    def test_python_demo_before_vs_after(self):
        r = subprocess.run([sys.executable, str(ROOT / "examples" / "run_demo.py"), "python"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        scores = [int(s) for s in re.findall(r"Mutation score: (\d+)%", r.stdout)]
        self.assertEqual(len(scores), 2, r.stdout)
        self.assertLess(scores[0], 40)
        self.assertGreaterEqual(scores[1], 90)
        self.assertEqual(r.stdout.count("working tree identical after the run: yes"), 2)

    @unittest.skipUnless(shutil.which("node"), "node not installed")
    def test_js_demo_before_vs_after(self):
        r = subprocess.run([sys.executable, str(ROOT / "examples" / "run_demo.py"), "js"],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        scores = [int(s) for s in re.findall(r"Mutation score: (\d+)%", r.stdout)]
        self.assertEqual(len(scores), 2, r.stdout)
        self.assertLess(scores[0], 40)
        self.assertGreaterEqual(scores[1], 90)

    def test_demo_tests_are_green_on_their_own(self):
        py = ROOT / "examples" / "python-demo"
        for folder in ("tests_weak", "tests_strong"):
            r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", folder],
                               cwd=py, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)

    def test_strong_python_test_fails_on_the_buggy_code(self):
        import tempfile
        from pathlib import Path
        py = ROOT / "examples" / "python-demo"
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copy(py / "before" / "pricing.py", Path(tmp) / "pricing.py")
            shutil.copy(py / "tests_strong" / "test_pricing.py", Path(tmp) / "test_pricing.py")
            r = subprocess.run([sys.executable, "-m", "unittest", "test_pricing"], cwd=tmp,
                               capture_output=True, text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("test_exactly_ten_units_get_ten_percent_off", r.stderr)


if __name__ == "__main__":
    unittest.main()
