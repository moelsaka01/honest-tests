# Changelog

## Unreleased

- **smell_scan.py fixes**, found by mutation-checking our own scanner (its tests scored 43%, now 87%): `typeof`-only JS assertions were never flagged (a stray backspace had replaced `\b` in a regex); `@patch` decorators were counted twice, so `mock-heavy` fired at two; `assert m.called` slipped through both mock-only and weak-assert; a bare `assert f()` is now weak like `assertTrue(f())`; a snapshot-only test is no longer reported as having no assertion. New exact-expectation tests for every rule, and a repo-wide guard against control characters in source.
- **Self-check of `mutation_check.py`** with the whole suite as the test command: 55% on a 60-mutant sample, 21 of 27 survivors now killed by `tests/test_selfcheck_gaps.py` (lexer edge cases, merge-base `--base`, exit codes, `pid_alive`, workdir copy, report output); one provably equivalent survivor recorded in `.mutation-ignore`. Two fallback checks (TypeScript `node -e` check, Node stripper branch) are still untested. The suite now runs in a private temp directory, so a mutated copy of the tool can no longer delete the backups of the run that is testing it.

## 0.2.0

- **JS/TS mutator rewritten** as a tokenizer with bracket and statement structure (was line-based): multi-line conditions, `a<b` without spaces, `${...}` expressions, arrow bodies, brace-less `if (x) save();`, ASI-aware `return`, TypeScript generics and type-only statements. Mutants of the demo source and a deliberately tricky file are all checked with `node --check`.
- **Type-aware TypeScript**: with the project's `typescript`, a persistent language service type-checks each mutant (the file and the files importing it) and drops the ones that no longer compile; only errors the mutant adds count (`--no-typecheck` to skip). Without TypeScript, Node's built-in type stripper (22.13+) is used for an exact parse.
- **Real TypeScript parser when available**: if the project has `typescript` installed, it identifies types, declarations, imports and JSX markup exactly (nothing is installed or downloaded by this tool), and every TS mutant is syntax-checked with it. Tested against the real compiler in CI.
- **`--jobs N`**: test mutants in parallel copies of the project; the project itself is never touched. Each copy is verified (suite passes; suite fails when sources are broken) and the run falls back to sequential when it cannot be trusted.
- **Equivalent mutants**: provably identical Python mutants are skipped automatically; `.mutation-ignore` accepts reasoned survivors (reason required, stale entries reported); `# pragma: no mutate`.
- **Crash recovery**: originals are backed up; Ctrl+C, SIGTERM, Ctrl+Break and, on Windows, console close / logoff / shutdown restore immediately; after SIGKILL the next run self-heals untouched mutants (a file you edited since is never overwritten); `--restore` forces it.
- `--show`, `--print-ignore`, `--color`, `--restore`; recorded demo; CI matrix on Ubuntu, Windows and macOS.

## 0.1.0

- Initial release: five skills, `mutation_check.py`, `smell_scan.py`, Python and JS demos.
