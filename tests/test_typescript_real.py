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

    def run_check(self, tests, tsconfig=None):
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
            if tsconfig:
                (d / "tsconfig.json").write_text(tsconfig, encoding="utf-8")
            before = (d / "price.ts").read_bytes()
            r = subprocess.run([sys.executable, str(_paths.MUTATION_CHECK), "--json", "--cmd",
                                "node --experimental-strip-types --no-warnings --test tests/price.test.ts"],
                               cwd=d, capture_output=True, text=True, env=dict(os.environ, HONEST_TESTS_TYPESCRIPT=TSJS))
            self.assertEqual((d / "price.ts").read_bytes(), before)
            return json.loads(r.stdout)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_mutants_that_stop_type_checking_are_not_counted(self):
        cfg = ('{"compilerOptions":{"strict":true,"noEmit":true,"target":"es2022","module":"esnext",'
               '"moduleResolution":"bundler","allowImportingTsExtensions":true},"include":["*.ts","tests/*.ts"]}')
        strict = self.run_check(self.WEAK, cfg)
        loose = self.run_check(self.WEAK)
        self.assertGreater(strict["invalid"], loose["invalid"])  # `return undefined` in a `: number` function
        self.assertEqual(strict["survived"] + strict["killed"] + strict["invalid"], len(strict["mutants"]))

    def test_weak_and_strong_typescript_suites(self):
        weak = self.run_check(self.WEAK)
        self.assertLess(weak["score"], 60, weak)
        self.assertEqual(weak["invalid"], 0)
        self.assertFalse([m for m in weak["mutants"] if m["line"] == 1])  # the `type Row` line is never mutated
        strong = self.run_check(self.STRONG)
        self.assertGreaterEqual(strong["score"], 85, strong)
        self.assertEqual(strong["invalid"], 0)


@unittest.skipUnless(TSJS, "typescript is not installed")
class TypeCheckTests(unittest.TestCase):
    """A mutant that still parses but no longer type-checks is a compile error, not a surviving bug."""

    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="ht-tc-"))
        (self.d / "tsconfig.json").write_text(
            '{"compilerOptions":{"strict":true,"noEmit":true,"target":"es2022","module":"esnext",'
            '"moduleResolution":"bundler"},"include":["*.ts"]}', encoding="utf-8")
        (self.d / "a.ts").write_bytes(b"export const n: number = 1 + 1;\nexport function f(x: number): number { return x + 1; }\n")
        (self.d / "b.ts").write_bytes(b"import { n } from './a';\nconst s: number = n;\nconst preexisting: string = 5;\n")
        self.chk = mc.TsChecker(NODE, TSJS, self.d)

    def tearDown(self):
        self.chk.close()
        shutil.rmtree(self.d, ignore_errors=True)

    def test_reports_the_version_and_config(self):
        self.assertRegex(self.chk.version, r"^\d+\.\d+")
        self.assertTrue(self.chk.config.endswith("tsconfig.json"))

    def test_harmless_mutant_adds_no_errors_and_preexisting_errors_are_ignored(self):
        text = (self.d / "a.ts").read_text(encoding="utf-8").replace("1 + 1", "1 - 1")
        self.assertEqual(self.chk.new_errors(self.d / "a.ts", text), [])

    def test_mutant_that_breaks_its_own_types_is_caught(self):
        text = (self.d / "a.ts").read_text(encoding="utf-8").replace("return x + 1;", "return undefined;")
        self.assertTrue(self.chk.new_errors(self.d / "a.ts", text))

    def test_mutant_that_breaks_a_file_that_imports_it_is_caught(self):
        text = (self.d / "a.ts").read_text(encoding="utf-8").replace("n: number = 1 + 1", "n = 'x'")
        errs = self.chk.new_errors(self.d / "a.ts", text)
        self.assertTrue(any(e.startswith("b.ts:") for e in errs), errs)

    def test_checker_leaves_no_trace_on_disk(self):
        before = (self.d / "a.ts").read_bytes()
        self.chk.new_errors(self.d / "a.ts", "export const n: number = 'x';\n")
        self.assertEqual((self.d / "a.ts").read_bytes(), before)
        self.assertEqual(self.chk.new_errors(self.d / "a.ts", before.decode("utf-8")), [])


def node_can_strip():
    if not NODE:
        return False
    return subprocess.run([NODE, "--no-warnings", "-e", "require('node:module').stripTypeScriptTypes('let a: number')"],
                          capture_output=True).returncode == 0


@unittest.skipUnless(node_can_strip(), "needs Node 22.13+/23.2+ (module.stripTypeScriptTypes)")
class NodeStripTierTests(unittest.TestCase):
    """No TypeScript installed anywhere: Node's own type stripper still gives an exact parse."""

    def test_types_are_blanked_with_the_same_length(self):
        d = Path(tempfile.mkdtemp(prefix="ht-ns-"))
        try:
            p = d / "a.ts"
            src = SAMPLE_TS.split("function g<T>")[0].replace("declare function ext(a: number): void;\n", "")
            p.write_bytes(src.encode("utf-8"))
            blank = mc.node_strip_types(NODE, p, src)
            self.assertIsNotNone(blank)
            self.assertEqual(len(blank), len(src))
            sites = mc.js_sites(src, None, ts=True, blanked=blank)
            lines = {s["line"] for s in sites}
            self.assertEqual(lines & {2, 3, 4, 6}, set())  # type alias, interface, overload, generic annotations
            self.assertIn(7, lines)                        # `a < b && c > d` is still mutated
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_run_uses_the_node_tier_when_typescript_is_absent(self):
        d = Path(tempfile.mkdtemp(prefix="ht-nr-"))
        try:
            (d / "p.ts").write_bytes(b"type T = { n: 5 };\nexport const f = (a: number): number => a + 1;\n")
            env = {k: v for k, v in os.environ.items() if k != "HONEST_TESTS_TYPESCRIPT"}
            r = subprocess.run([sys.executable, str(_paths.MUTATION_CHECK), "--files", "p.ts", "--cmd", "node -e 0", "--list"],
                               cwd=d, capture_output=True, text=True, env=env)
            self.assertIn("Node's built-in type stripper", r.stdout, r.stdout + r.stderr)
            self.assertNotIn("p.ts:1 ", r.stdout)
            self.assertIn("p.ts:2 ", r.stdout)
        finally:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
