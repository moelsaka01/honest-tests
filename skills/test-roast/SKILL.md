---
name: test-roast
description: Use when asked to review, audit, critique, or "roast" a test suite, a test file, or a pull request's tests, or to judge whether the tests can be trusted (are they just green?). Produces a blunt, prioritised review with exact fixes and a 0-10 trust score, backed by a static smell scanner and by mutation-check. Works for Python and JavaScript/TypeScript.
---

# Test Roast

Be the reviewer who asks: "If I broke this code on purpose, would these tests notice?" Kindly, specifically, with fixes.

`<skill-dir>` means the folder that contains this SKILL.md. The scanner uses only the standard library.

## Process

1. **Run the scanner:**
   ```bash
   python <skill-dir>/scripts/smell_scan.py tests/ --diff       # --diff adds the test changes vs HEAD
   ```
   It parses Python with `ast` and JS/TS with a brace-matching scanner, and reports with file and line and a fix:
   - **BAD** (the test cannot fail or was weakened): `no-assert`, `weak-assert`, `tautology`, `mock-only`, `swallowed-exception`, `skipped`, and in diff mode `deleted-test`, `added-skip`, `loosened`.
   - **SUSPECT** (needs a human look): `patches-unit`, `mock-heavy`, `snapshot`.
   Exit code `1` means at least one BAD hit. `--json` gives structured output. Files it cannot parse are listed as `NOT SCANNED`, never skipped silently. It is a **smell detector, not a proof**.

2. **Prove it with mutation testing.** Run `mutation-check` on the code the tests cover (`--base <ref>` for a branch, `--files` for a module). The mutation score is the objective evidence; the scanner explains it.

3. **Then read the tests yourself**, most damaging first:
   1. Tests that mock the unit under test, or assert only on mocks (`mock-audit`).
   2. Weak or missing assertions, no boundaries, no error cases (`assertion-strength`).
   3. Tests that were skipped, deleted, or loosened to get green.
   4. Tests coupled to implementation details that will break on a harmless refactor.
   5. Flaky inputs: time, randomness, ordering, network, shared state between tests.
   6. Missing cases: empty input, `None`/`null`, negative numbers, the other side of every branch.

## Trust score rubric

| Score | Meaning |
|---|---|
| 9-10 | Mutation score 90%+ on the reviewed code, scanner clean, boundaries and errors covered. |
| 7-8 | Mutation score 70-89%, only minor smells. |
| 5-6 | Mutation score 40-69%, or several weak/mock-only tests; the suite catches blatant breaks only. |
| 3-4 | Mutation score 20-39%, or tests were skipped/deleted/loosened in the change. Do not trust "green". |
| 0-2 | Mutation score under 20%, or most tests have no real assertion. Green means nothing. |

The scanner prints a "scan score". Use it as a floor-check, not as your answer.

## Format

Group findings by severity (Fatal, Serious, Minor). For each: file and line, what is wrong in one sentence, why it matters, and the exact fixed assertion as code. End with the **trust score** and the single most important fix. One line on what is done well is enough.

## Output contract

```
Test roast: <paths reviewed>
- Scan: <n> tests, <n> flagged | mutation-check: <pct>% on <n> mutants
- Fatal: <file:line - problem - fix>
- Serious: <file:line - problem - fix>
- Minor: <file:line - problem - fix>
- Trust score: <0-10>/10 - <one-sentence reason>
- Most important fix: <one line>
```

## Works with

`test-roast` is the reviewer for the other four skills: `mutation-check` (proof), `mock-audit`, `assertion-strength`, and `done-means-verified` (the claim that work is finished).
