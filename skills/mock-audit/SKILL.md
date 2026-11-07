---
name: mock-audit
description: Use when writing, reviewing, or fixing tests that use mocks, patches, stubs, spies, jest.mock, vi.mock, unittest.mock, or MagicMock, and whenever a test asserts only that a mock was called. Flags tests that mock the unit under test, assert only on mocks, or mock so much that no real logic runs, and replaces them with real dependencies or small fakes. Includes a static scanner for Python and JavaScript/TypeScript.
---

# Mock Audit

A test that mocks the thing it tests is a test of the mock. It passes whatever the code does.

`<skill-dir>` means the folder that contains this SKILL.md. The scanner is `../test-roast/scripts/smell_scan.py` (standard library only).

## Steps

1. **Scan:**
   ```bash
   python <skill-dir>/../test-roast/scripts/smell_scan.py tests/ --group mock
   ```
   It reports, with file and line: `mock-only` (every assertion is `assert_called*`, `toHaveBeenCalled*`, call counts), `patches-unit` (the test patches a function and then calls that same function), `mock-heavy` (three or more mocks in one test). It is a smell detector; open every hit.

2. **For each test that uses a mock, answer three questions:**
   1. *What real code runs?* If the answer is "none", the test is worthless. Rewrite it.
   2. *What would fail if the code under test returned the wrong value?* If the only check is "was the mock called", the answer is nothing.
   3. *Is the mock replacing a boundary (network, clock, filesystem, payment API, randomness) or replacing logic?* Mock boundaries only. Never mock a function in the module you are testing, or a pure helper it calls.

3. **Fix by preference:**
   1. Use the **real dependency** (in-memory DB, temp dir, real parser).
   2. Use a **fake**: a small hand-written class with the same interface and real behaviour (`FakeClock`, `InMemoryRepo`).
   3. Use a **stub that returns canned data** at the boundary, and assert on the *result* of the code under test, not on the stub.
   4. Keep a call-assertion only when the call *is* the behaviour (an email was sent, a charge was created), and pair it with an assertion on the arguments (`assert_called_once_with(...)`, `toHaveBeenCalledWith(...)`) plus an assertion on the returned result.

4. **Prove it.** Run `mutation-check` on the changed source. A mock-heavy test that still lets mutants survive is not fixed.

## Red flags

- `patch("pkg.module.the_function_being_tested")`
- `mock.return_value = X` followed by `assert result == X`
- `assert_called_once()` as the only assertion
- A `MagicMock` standing in for a domain object whose real constructor is cheap
- More lines of mock setup than lines of assertions

## Output contract

```
Mock audit: <files scanned>
- Tests using mocks: <n>
- Mocks of the unit under test: <list or "none">
- Mock-only tests: <list or "none">
- Replaced with: real dependency <n> | fake <n> | boundary stub <n> | kept (call is the behaviour) <n>
- Real code path exercised by each remaining mock test: <one line each>
- mutation-check score after the changes: <pct>%
```

## Works with

`assertion-strength` covers the non-mock weak assertions. `mutation-check` proves the rewritten tests fail on real bugs.
