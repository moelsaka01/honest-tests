---
name: done-means-verified
description: Use before saying a bug is fixed, a feature works, tests pass, or the task is done. Requires red-to-green proof: a test that fails on the old code and passes on the new code, the actual test command output pasted, and a mutation-check score. Also applies to refactors (tests unchanged and green) and to reports of what was and was not run.
---

# Done Means Verified

"Fixed" is a claim. This skill is the evidence for it. Do not write "fixed", "done", "works", or "all tests pass" until every step below has real output.

`<skill-dir>` means the folder that contains this SKILL.md.

## Steps

1. **Reproduce first (red).** Before changing the code, write the test that expresses the bug or feature. Run it against the *current* code and confirm it fails *for the right reason* (the assertion about the behaviour fails, not an import error or typo). Keep the failing output.

2. **Change the code (green).** Make the smallest change that fixes it. Run the same test: it must pass. Keep the output.

3. **Prove the test is the one that matters.** If the change is already made, stash only the source change and show the test fail:
   ```bash
   git stash push -- path/to/source.py      # source only, keep the new test
   <test command>                            # must FAIL
   git stash pop
   <test command>                            # must PASS
   ```
   For a bug that is not reproducible by a test (UI, infra), say so and show the manual evidence instead of implying a test exists.

4. **Run the whole suite**, not just the new test, with the real command, and quote the summary line (`12 passed in 0.4s`). Quote failures if there are any; do not paraphrase them away. Note anything you did not run (integration tests, other platforms, other Python versions).

5. **Mutation-check the change** (`mutation-check` skill). Survivors on the lines you changed mean the test does not pin the fix. Strengthen it.

6. **Report honestly.** If a test was skipped, deleted, loosened, or a failure was left in place, say so first. If you could not run something, say that instead of implying it passed.

## Banned phrases without evidence

"This should fix it", "tests pass" (which tests, which command?), "I verified" (what did you run?), "all green" (after how many tests were skipped?).

## Output contract

```
Verified: <one-line description of the change>
- Red: `<command>` on the old code -> <failing test name> failed with "<assertion message>"
- Green: `<command>` on the new code -> <pass summary line>
- Full suite: `<command>` -> <summary line>   | not run: <list or "nothing">
- Mutation check: <score>% on <n> mutants, survivors: <none | justified list>
- Tests skipped, deleted or loosened: <list with reasons, or "none">
- Not verified / known gaps: <list, or "none">
```

## Works with

`mutation-check` gives the score, `assertion-strength` and `mock-audit` fix tests that cannot fail, `test-roast` reviews the whole suite.
