# Changelog

## 0.2.0

- **JS/TS mutator rewritten** as a tokenizer with bracket and statement structure (was line-based): multi-line conditions, `a<b` without spaces, `${...}` expressions, arrow bodies, brace-less `if (x) save();`, ASI-aware `return`, TypeScript generics and type-only statements. Mutants of the demo source and a deliberately tricky file are all checked with `node --check`.
- **TypeScript mutants** are validated with the project's own `typescript` package when installed.
- **`--jobs N`**: test mutants in parallel copies of the project; the project itself is never touched. Each copy is verified (suite passes; suite fails when sources are broken) and the run falls back to sequential when it cannot be trusted.
- **Equivalent mutants**: provably identical Python mutants are skipped automatically; `.mutation-ignore` accepts reasoned survivors (reason required, stale entries reported); `# pragma: no mutate`.
- **Crash recovery**: originals are backed up; Ctrl+C, SIGTERM, Ctrl+Break and, on Windows, console close / logoff / shutdown restore immediately; `--restore` recovers from SIGKILL.
- `--show`, `--print-ignore`, `--color`, `--restore`; recorded demo; CI matrix on Ubuntu, Windows and macOS.

## 0.1.0

- Initial release: five skills, `mutation_check.py`, `smell_scan.py`, Python and JS demos.
