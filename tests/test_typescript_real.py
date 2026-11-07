"""Against the real TypeScript compiler. Runs when `typescript` is installed (HONEST_TESTS_TYPESCRIPT, or
node_modules/typescript in or above the repo); CI installs it, so these never silently disappear there."""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import _paths
from test_mutation_check import git, mc

NODE = shutil.which("node")
TSJS = mc.find_typescript(_paths.ROOT) if NODE else None


def node_major():
    try:
        return int(re.search(r"v(\d+)", subprocess.run([NODE, "--version"], capture_output=True, text=True).stdout).group(1))
    except Exception:
        return 0


SAMPLE_TS = """import { x } from './x';
type Fn = (x: number) => number;
interface P { n: 5; f: (a: number) => void }
declare function ext(a: number): void;
function over(a: number): number;
function over(a: any): any { return a + 1 }
const m: Map<string, Array<number>> = new Map<string, Array<number>>();
const ok = a < b && c > d;
function g<T>(x: T): Promise<void> { return h<T>(x) }
const f = (n: number): number => n * 2;
const v = (y as number) + 1;
"""
SAMPLE_TSX = "const A = () => <div className=\"a\" onClick={() => count + 1}>it's {n > 3 ? 'big' : 'small'} here</div>;\n"


@unittest.skipUnless(TSJS, "typescript is not installed")
class RealTypeScriptTests(unittest.TestCase):
    def sites(self, name, src):
        d = Path(tempfile.mkdtemp(prefix="ht-tsr-"))
        try:
            p = d / name
            p.write_bytes(src.encode("utf-8"))
            ex = mc.ts_exclusions(NODE, TSJS, p, src)
            self.assertIsNotNone(ex)
            return mc.js_sites(src, None, ts=True, exclude=ex)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_types_declarations_and_imports_are_never_mutated(self):
        lines = {s["line"] for s in self.sites("a.ts", SAMPLE_TS)}
        self.assertEqual(lines & {1, 2, 3, 4, 5, 7}, set())
        self.assertEqual(lines, {6, 8, 9, 10, 11})

    def test_comparisons_next_to_generics_are_still_mutated(self):
        labels = [s["label"] for s in self.sites("a.ts", SAMPLE_TS) if s["line"] == 8]
        self.assertIn("`<` -> `>=`", labels)
        self.assertIn("`>` -> `<=`", labels)

    def test_jsx_markup_is_skipped_but_expressions_are_mutated(self):
        labels = [s["label"] for s in self.sites("b.tsx", SAMPLE_TSX)]
        self.assertIn("`>` -> `<=`", labels)  # in {n > 3 ? ...}, after JSX text containing an apostrophe
        self.assertIn("`+` -> `-`", labels)   # in the onClick expression
        self.assertFalse(any("className" in s["label"] for s in self.sites("b.tsx", SAMPLE_TSX)))

    def test_non_bmp_characters_do_not_shift_the_ranges(self):
        src = "const s = '\U0001F600';\ntype T = number;\nconst z = 1 + 2;\n"
        sites = self.sites("c.ts", src)
        self.assertEqual({s["line"] for s in sites}, {1, 3} - {1} | set())  # only line 3 is runtime code
        out = mc.js_apply(src, [s for s in sites if s["label"] == "`+` -> `-`"][0])
        self.assertIn("1 - 2", out)

    def test_every_mutant_parses_with_the_real_compiler(self):
        d = Path(tempfile.mkdtemp(prefix="ht-tsv-"))
        try:
            src = SAMPLE_TS
            p = d / "a.ts"
            p.write_bytes(src.encode("utf-8"))
            ex = mc.ts_exclusions(NODE, TSJS, p, src)
            bad = []
            for s in mc.js_sites(src, None, ts=True, exclude=ex):
                p.write_bytes(mc.js_apply(src, s).encode("utf-8"))
                if subprocess.run([NODE, "-e", mc.TS_CHECK_JS, TSJS, str(p)], capture_output=True).returncode:
                    bad.append(s["label"])
            self.assertEqual(bad, [])
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_syntax_check_rejects_broken_code(self):
        d = Path(tempfile.mkdtemp(prefix="ht-tsb-"))
        try:
            (d / "ok.ts").write_text("const a: number = 1;\n", encoding="utf-8")
            (d / "bad.ts").write_text("const a: number = ;\n", encoding="utf-8")
            run = lambda f: subprocess.run([NODE, "-e", mc.TS_CHECK_JS, TSJS, str(d / f)], capture_output=True).returncode
            self.assertEqual(run("ok.ts"), 0)
            self.assertEqual(run("bad.ts"), 1)
        finally:
            shutil.rmtree(d, ignore_errors=True)


@unittest.skipUnless(TSJS and node_major() >= 22, "needs typescript and Node 22+ (runs .ts directly)")
class EndToEndTypeScriptTests(unittest.TestCase):
    SRC = """export type Row = { qty: number; price: number };

export function discount(qty: number, price: number): number {
  const rate: number = qty >= 10 ? 0.1 : 0;
  return price * qty * (1 - rate);
}
"""
    WEAK = """import test from 'node:test';
import assert from 'node:assert/strict';
import { discount } from '../price.ts';

test('discount is a number', () => {
  assert.ok(discount(1, 10) >= 0);
  assert.ok(discount(10, 10) > 0);
});
"""
    STRONG = """import test from 'node:test';
import assert from 'node:assert/strict';
import { discount } from '../price.ts';

test('no discount below 10', () => {
  assert.equal(discount(1, 10), 10);
  assert.equal(discount(9, 10), 90);
  assert.equal(discount(0, 10), 0);
});
test('10 percent from 10', () => {
  assert.equal(discount(10, 10), 90);
  assert.equal(discount(11, 10), 99);
});
"""

    def run_check(self, tests):
        d = Path(tempfile.mkdtemp(prefix="ht-tse-"))
        try:
            git(d, "init", "-q")
            git(d, "config", "user.email", "t@example.com")
            git(d, "config", "user.name", "t")
            (d / "price.ts").write_text("export function discount(qty: number, price: number): number { return 0; }\n",
                                        encoding="utf-8")
            git(d, "add", "-A")
            git(d, "commit", "-qm", "base")
            (d / "price.ts").write_text(self.SRC, encoding="utf-8")
            (d / "tests").mkdir()
            (d / "tests" / "price.test.ts").write_text(tests, encoding="utf-8")
            before = (d / "price.ts").read_bytes()
            r = subprocess.run([sys.executable, str(_paths.MUTATION_CHECK), "--json", "--cmd",
                                "node --experimental-strip-types --no-warnings --test tests/price.test.ts"],
                               cwd=d, capture_output=True, text=True, env=dict(os.environ, HONEST_TESTS_TYPESCRIPT=TSJS))
            self.assertEqual((d / "price.ts").read_bytes(), before)
            return json.loads(r.stdout)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_weak_and_strong_typescript_suites(self):
        weak = self.run_check(self.WEAK)
        self.assertLess(weak["score"], 60, weak)
        self.assertEqual(weak["invalid"], 0)
        self.assertFalse([m for m in weak["mutants"] if m["line"] == 1])  # the `type Row` line is never mutated
        strong = self.run_check(self.STRONG)
        self.assertGreaterEqual(strong["score"], 85, strong)
        self.assertEqual(strong["invalid"], 0)


if __name__ == "__main__":
    unittest.main()
