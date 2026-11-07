---
name: mutation-check
description: Use after writing, changing, or fixing tests or the code they cover, and before saying tests are done, pass, strong, or sufficient. Proves the tests can fail by mutating only the lines in your git diff (flipped comparisons, swapped and/or, + and -, true/false, off-by-one constants, early return, removed calls) and running the suite for each mutant. Any surviving mutant means a test is too weak. Includes a runnable script for Python and JavaScript/TypeScript.
---

# Mutation Check

A green test run proves nothing until you have seen the tests go red for a real bug. This skill makes them go red on purpose, on exactly the code you just touched.

`<skill-dir>` means the folder that contains this SKILL.md. The script uses only the Python 3.9+ standard library, plus `git`. It runs your own test command.

## Steps

1. **Get the suite green first.** The script aborts with exit code 2 if the unmutated code fails its tests.

2. **Run it on your change:**
   ```bash
   python <skill-dir>/scripts/mutation_check.py                       # diff vs HEAD, test command auto-detected
   python <skill-dir>/scripts/mutation_check.py --base origin/main    # everything on this branch
   python <skill-dir>/scripts/mutation_check.py --cmd "pytest -q -x" --min-score 80
   python <skill-dir>/scripts/mutation_check.py --files src/pricing.py   # no git diff: mutate whole files
   ```
   It reads `git diff` (untracked files count), keeps only changed lines of non-test source files, applies one small mutation at a time, runs the test command with a per-mutant timeout, and restores the file after every mutant, on errors, on Ctrl+C and on SIGTERM. Use a fail-fast test command (`pytest -x`, `node --test`) to keep it quick. `--list` shows the mutants without running them; `--json` is machine-readable; `--max-mutants` (default 80) samples large diffs; `-v` also lists killed mutants.

3. **Read the survivors.** Each line is `file:line  mutation  | source`. A survivor means the suite passes with that bug in place. For every survivor, do exactly one of:
   - **Kill it:** write or tighten a test that fails when that change is applied (an exact expected value at the boundary, an assertion on the side effect, a case for the other branch). Re-run.
   - **Justify it:** if the mutant cannot change behaviour (equivalent mutant: a dead default, a log message, a value that is overwritten), run with `--print-ignore`, paste its line into `.mutation-ignore` and replace `REASON` with a one-sentence reason (`file | mutation | source  # reason`; entries without a reason are rejected). Provably identical Python mutants are skipped for you. `# pragma: no mutate` on a line (`// pragma: no mutate` in JS) skips every mutant on it; prefer the ignore file because it keeps the reason.
   Never kill a survivor by editing the source, deleting the mutated line, or mocking the code.

4. **Repeat until the score meets the threshold** (default 80%, `--min-score`). Then, and only then, say the tests are good.

5. **Check the repo is clean.** The script reports "Source files restored: yes". Confirm with `git status` that only your intended changes remain. If a run was killed (SIGKILL, closed terminal) the next run refuses to start; run `python <skill-dir>/scripts/mutation_check.py --restore` first.

## What it does not prove

Mutation score measures whether tests *notice small code changes on the lines you changed*. It does not prove the tests check the right behaviour, cover untouched code, or that the requirements are correct. Python mutants come from `ast`; JS/TS mutants come from a comment- and string-aware line mutator, so unusual formatting can hide mutation sites (the script then reports fewer mutants, never wrong ones; a mutant that does not compile is dropped, never counted as killed).

## Output contract

End the task with this block, filled from the script's real output:

```
Mutation check: <command run>
- Tests before mutation: <n> passing
- Mutants: <valid> | killed <k> | survived <s> | not compiled <i>
- Score: <pct>% (threshold <min>%)
- Survivors fixed: <file:line mutation -> the test that now kills it>
- Survivors accepted as equivalent: <file:line mutation -> reason recorded in .mutation-ignore>   (or "none")
- Repo restored clean: yes/no (git status checked)
```

## Works with

`assertion-strength` and `mock-audit` find *why* a survivor survived. `done-means-verified` requires this score before "fixed" is claimed. `test-roast` uses it as the proof behind its trust score.
