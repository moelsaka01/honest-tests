---
name: assertion-strength
description: Use when writing or editing test assertions, and when any test diff adds skip, xfail, a try/except, a snapshot, or a looser assertion to get green. Catches assert result, assert x is not None, assertTrue(x), toBeTruthy, toBeDefined, type-only checks, assertion-free tests, unreviewed snapshots, exceptions swallowed in tests, skipped or deleted tests, and assertions loosened in the same diff. Includes a static scanner with a git-diff mode.
---

# Assertion Strength

A strong assertion names the exact value the code must produce. If a wrong implementation could still satisfy the assertion, it is not an assertion.

`<skill-dir>` means the folder that contains this SKILL.md. The scanner is `../test-roast/scripts/smell_scan.py` (standard library only).

## Steps

1. **Scan the tests and the test diff:**
   ```bash
   python <skill-dir>/../test-roast/scripts/smell_scan.py tests/ --diff --group assertion
   ```
   Rules: `no-assert`, `weak-assert` (every check is truthy, `is not None`, `isinstance`, `> 0`, `toBeDefined`, `typeof`), `tautology` (`assert True`, `expect(x).toBe(x)`), `swallowed-exception` (try/except or try/catch with no assertion), `skipped`, `snapshot`, and in `--diff` mode `deleted-test`, `added-skip`, `loosened` (strong assertions removed or replaced by weak ones in one hunk). Exit code 1 means at least one BAD finding.

2. **Rewrite every flagged assertion:**

   | Weak | Strong |
   |---|---|
   | `assert result` | `assert result == {"id": 7, "total": 90.0}` |
   | `assert x is not None` | `assert x == expected` (keep `is not None` only when "absent" is the bug) |
   | `assertTrue(len(items))` | `assertEqual(items, ["a", "b"])` |
   | `expect(fn()).toBeTruthy()` | `expect(fn()).toBe(42)` |
   | `try: f() except: pass` | `with pytest.raises(ValueError, match="qty"):` |
   | `toMatchSnapshot()` alone | read the snapshot, then assert the 1-2 fields that matter |
   | `assert abs(a-b) < 100` | `pytest.approx(b, rel=1e-6)` with a tolerance justified by the maths |

3. **Test boundaries, not only the middle.** For every comparison in the changed code (`>=`, `<`, `==`) assert the value on each side of the edge (9, 10, 11 for `>= 10`). For every branch, one test that takes each side. For every raised error, one test that expects that error type and message.

4. **Never make a test green by loosening it.** If a test fails after your change, one of three things is true: the code is wrong (fix the code), the requirement changed (change the expected value and say so explicitly), or the test was flaky (fix the cause). Adding `skip`, `xfail`, `.only`, a wider tolerance, a catch-all `except`, or deleting the test is not a fix. If you must remove or skip a test, say which one and why, in the final message.

5. **Prove it.** Run `mutation-check`. Weak assertions are exactly what produce surviving mutants.

## Output contract

```
Assertion strength: <files scanned>
- Tests scanned: <n> | flagged: <n>
- Rewritten: <test name -> the exact assertion now used>
- Skipped/xfail/deleted tests in this change: <list with a reason each, or "none">
- Assertions loosened in this change: <list, or "none">
- Boundaries asserted: <comparison -> values on each side>
- Scanner exit code after the changes: <0|1> | mutation-check score: <pct>%
```

## Works with

`mock-audit` handles mock-only tests. `done-means-verified` requires a red-to-green proof. `mutation-check` is the objective test of assertion strength.
