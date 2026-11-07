"""The JavaScript/TypeScript mutator: tokenizer-level behaviour, and that its mutants really compile."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import _paths
from test_mutation_check import mc

NODE = shutil.which("node")


def labels(src, ts=False, changed=None):
    return [s["label"].split(" (#")[0] for s in mc.js_sites(src, changed, ts=ts)]


def mutants(src, ts=False):
    return [(s["label"], mc.js_apply(src, s)) for s in mc.js_sites(src, None, ts=ts)]


class LexerTests(unittest.TestCase):
    def kinds(self, src):
        return [(t.k, t.t) for t in mc.js_lex(src)]

    def test_regex_versus_division(self):
        self.assertEqual(self.kinds("a / b")[1], ("p", "/"))
        self.assertEqual(self.kinds("x = /a\\/b[/]/g.test(s)")[2][0], "re")
        self.assertEqual(self.kinds("f(x) / 2")[4], ("p", "/"))
        self.assertEqual(self.kinds("return /ab+/")[1][0], "re")

    def test_nested_template_literals_keep_their_expressions(self):
        toks = self.kinds("`a ${b + `c ${d}`} e`")
        self.assertIn(("id", "b"), toks)
        self.assertIn(("id", "d"), toks)
        self.assertEqual(sum(1 for k, _ in toks if k == "tpl"), 4)  # head, inner head, inner tail, tail

    def test_comments_and_line_numbers(self):
        toks = mc.js_lex("a /* x\n y */ b // c\nd")
        self.assertEqual([(t.t, t.line) for t in toks], [("a", 1), ("b", 2), ("d", 3)])


class SiteTests(unittest.TestCase):
    def test_multiline_condition_and_asi_return(self):
        src = "function f(a, b) {\n  if (\n    a >= 10 &&\n    b === 3\n  ) return a\n    + b\n}\n"
        got = labels(src)
        for want in ("negate `if` condition", "`>=` -> `<`", "`&&` -> `||`", "`===` -> `!==`",
                     "`return <value>` -> `return undefined`", "`+` -> `-`"):
            self.assertIn(want, got)
        ret = [m for lab, m in mutants(src) if lab.startswith("`return <value>`")][0]
        self.assertIn("return undefined", ret)
        self.assertNotIn("+ b", ret)  # the whole two-line expression was replaced

    def test_operators_without_spaces_are_mutated(self):
        got = labels("const r = a<b && c>=d || e===f;\n")
        for want in ("`<` -> `>=`", "`>=` -> `<`", "`===` -> `!==`", "`&&` -> `||`", "`||` -> `&&`"):
            self.assertIn(want, got)

    def test_template_expressions_are_mutated_but_template_text_is_not(self):
        got = labels("const s = `total ${a + b} > 3 items`;\n")
        self.assertIn("`+` -> `-`", got)
        self.assertFalse(any(l.startswith("`3`") for l in got))
        self.assertFalse(any(l.startswith(("`<", "`>")) for l in got))

    def test_regex_literals_and_strings_are_not_mutated(self):
        self.assertEqual(labels("const re = /a+b > 1 && c/g;\nconst s = 'x == y';\n"), [])

    def test_arrow_body_and_nested_callbacks(self):
        got = labels("const t = items.map(i => i.price * i.qty);\n")
        self.assertIn("arrow body -> `undefined`", got)
        self.assertIn("`*` -> `/`", got)
        self.assertNotIn("arrow body -> `undefined`", labels("const f = () => undefined;\n"))

    def test_call_statements_in_brace_less_bodies_and_switch_cases(self):
        self.assertIn("remove call statement", labels("if (x) save(x);\n"))
        self.assertIn("remove call statement", labels("switch (k) {\n  case 1: save(k); break;\n}\n"))
        self.assertNotIn("remove call statement", labels("const v = cond ? save(1) : save(2);\n"))
        self.assertNotIn("remove call statement", labels("console.log(x);\nlogger.info(y);\n"))

    def test_unary_not_and_update_operators(self):
        got = labels("while (!done) { n++; }\nfor (let i = 0; i < n; i++) {}\n")
        self.assertIn("remove `!`", got)
        self.assertEqual(got.count("`++` -> `--`"), 1)  # not the loop counter

    def test_negative_replacement_is_parenthesised(self):
        out = [m for lab, m in mutants("const x = a - 0;\n") if lab == "`0` -> `-1`"]
        self.assertEqual(out, ["const x = a - (-1);\n"])

    def test_imports_and_exports_are_skipped(self):
        src = "import { a } from './a';\nexport * from './b';\nexport { c } from './c';\nconst d = 1 + 2;\n"
        self.assertEqual({s["line"] for s in mc.js_sites(src, None)}, {4})

    def test_typescript_generics_types_and_comparisons(self):
        src = ("type Fn = (x: number) => number;\ninterface P { n: 5; f: (a: number) => void }\n"
               "const m: Map<string, Array<number>> = new Map<string, Array<number>>();\n"
               "const ok = a < b;\nfunction g<T>(x: T): Promise<void> { return h<T>(x) }\n")
        got = mc.js_sites(src, None, ts=True)
        by_line = {}
        for s in got:
            by_line.setdefault(s["line"], []).append(s["label"])
        self.assertNotIn(1, by_line)
        self.assertNotIn(2, by_line)
        self.assertNotIn(3, by_line)
        self.assertIn("`<` -> `>=`", by_line[4])
        self.assertFalse([l for l in by_line.get(5, []) if l.startswith(("`<`", "`>`"))])

    def test_plain_js_treats_adjacent_angle_brackets_as_comparison(self):
        self.assertIn("`<` -> `>=`", labels("const ok = a<b;\n"))

    def test_only_changed_lines_and_pragma(self):
        src = "a = 1 + 2;\nb = 3 + 4;\nc = 5 + 6; // pragma: no mutate\n"
        self.assertEqual({s["line"] for s in mc.js_sites(src, {2, 3})}, {2})

    def test_crlf_and_bom_sources_round_trip(self):
        src = "﻿const a = 1 + 2;\r\nconst b = a > 3;\r\n"
        for _, out in mutants(src):
            self.assertTrue(out.startswith("﻿"))
            self.assertEqual(out.count("\r\n"), 2)


@unittest.skipUnless(NODE, "node not installed")
class MutantsCompileTests(unittest.TestCase):
    """The point of a real tokenizer: mutants should be valid programs, not guesses."""

    def check_all(self, src, suffix=".js"):
        d = Path(tempfile.mkdtemp(prefix="ht-js-"))
        try:
            bad = []
            for label, out in mutants(src):
                f = d / f"m{suffix}"
                f.write_text(out, encoding="utf-8")
                if subprocess.run([NODE, "--check", str(f)], capture_output=True).returncode != 0:
                    bad.append(label)
            return bad
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_demo_source_mutants_all_compile(self):
        src = (_paths.ROOT / "examples" / "js-demo" / "cart.js").read_text(encoding="utf-8")
        self.assertGreater(len(mutants(src)), 10)
        self.assertEqual(self.check_all(src), [])

    def test_tricky_syntax_mutants_all_compile(self):
        src = (
            "'use strict';\n"
            "const re = /[/]+x/g;\n"
            "function f(a, b, items) {\n"
            "  const half = a / 2 / b;\n"
            "  const msg = `n=${items.length > 0 ? `has ${items.length}` : 'none'}`;\n"
            "  if (a<b) save(a)\n"
            "  else if (\n    a === 0 ||\n    b === 0\n  ) { return -1 }\n"
            "  for (let i = 0; i < items.length; i++) { total += items[i].qty * 2 }\n"
            "  switch (a) { case 1: log(a); break; default: save(b) }\n"
            "  const o = { n: 5, g() { return !b }, h: x => x + 1 };\n"
            "  return items.filter(i => !i.hidden).map(i => i.qty - 1)\n"
            "}\n"
            "module.exports = { f, re };\n")
        self.assertEqual(self.check_all(src), [])


if __name__ == "__main__":
    unittest.main()
