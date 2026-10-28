# Guard conventions

A *guard* is a check that makes a convention impossible to break silently.
Four exist today, and all of them run in CI on every push:

| Guard | Enforces |
| --- | --- |
| [`dev/tools/verify_powershell_single_call_site.py`](dev/tools/verify_powershell_single_call_site.py) | PowerShell argv is built only in `app/powershell.py`, and the exe name is mapped to a path only in `app/exe_paths.py` |
| [`dev/tools/verify_sec001_task_allowlist.py`](dev/tools/verify_sec001_task_allowlist.py) | the per-task registry allowlist still matches what the tasks actually write |
| [`dev/tools/verify_dryrun_scope.py`](dev/tools/verify_dryrun_scope.py) | every task on `storage_scan.SCAN_ALLOWLIST` mutates nothing during a `dry_run` pass, and the dry-aware primitives it trusts still check `dry_run` |
| [`dev/tools/design_literals.py`](dev/tools/design_literals.py) | a widget picks a token, never a number: `radius`, `padx/pady`, icon sizes and font tuples come from `theme`/`metrics`. It prints every finding with the token to use, grouped by rule, and `dev/tests/test_design_literals.py` fails the suite on any of them |

Add one when a rule is (a) security- or data-safety-relevant, (b) easy to break
by accident while editing somewhere else, and (c) checkable from the source.

## The conventions

**1. One owner.** The rule lives in one place — `app/powershell.py` for the
argv, `_TASK_REG_ALLOWLIST` for the registry specs — and everything else
resolves through it. A guard is what keeps that true, not what implements it.

**2. Exceptions are declared in place, with a reason.** Never in the checker.
`# ps-ok: <why>` / `# dry-ok: <why>` on the line, or an entry in
`_TASK_REG_ALLOWLIST_NOT_A_TASK` beside the table it explains. An exception
without a reason is a failure, and so is one that outlives its subject (a
declared id no longer in the allowlist, say). If you cannot write the reason in
one sentence, the exception is probably wrong.

**3. Exceptions are printed, not hidden.** Every run lists each one with its
reason, so they get reviewed instead of accumulating. A silent allowlist is how
the rule quietly stops applying.

**4. Fail closed.** If the guard cannot verify something, that is a failure —
not a skip and not a pass. `_verify_prefix_rules` fails when a write loop stops
iterating literal names, because the alternative is a rule nobody is checking.

**5. Derive, don't pattern-match.** Read what the code *states* — the AST, the
tables, the actual call arguments. The SEC-001 report used to treat any
allowlist id ending in `_regs`/`_pmz`/`_specs` as "expected, internal"; a naming
convention was standing in for a decision, and an id nobody meant to add would
have passed. Suffix and filename heuristics are the thing these guards exist to
replace.

**6. Skip prose deliberately.** Docstrings and user-facing messages legitimately
contain the words a guard looks for ("PowerShell is not available."). Teach the
checker the difference — the PowerShell guard skips docstring constants and
requires the flag dash — instead of weakening the rule until the copy fits.

## Adding a guard

1. Write it in `dev/tools/verify_<subject>.py`, with a module docstring that says
   *why* the rule exists and what each failure means. Print a short report
   (counts + every exception) and return `1` on drift.
2. Put the rule's owner first; the checker only confirms it.
3. Add a step in [`.github/workflows/build.yml`](.github/workflows/build.yml)
   next to the existing three, and mention the command in the README's "Run
   Tests" block.
4. Add a test that **proves it fires**: plant the regression in a `tmp_path`
   copy or a synthetic package and assert the failure — plus the cases that must
   stay quiet (resolver arguments, copy, docstrings). See
   [`dev/tests/test_powershell_call_site_guard.py`](dev/tests/test_powershell_call_site_guard.py).
   A guard nobody has seen fire is a guard you cannot trust.
5. Run everything the CI gate runs:

   ```powershell
   python -m pytest -q
   python -m ruff check app dev --select F
   python dev/tools/design_literals.py
   python dev/tools/verify_sec001_task_allowlist.py
   python dev/tools/verify_powershell_single_call_site.py
   python dev/tools/verify_dryrun_scope.py
   ```

## When a guard fails

Read the message before changing anything. It names the file, the line, and
what the rule expected. The fix is almost always to route the new code through
the owner (`ps_argv`, an allowlist entry). Reach for an exception only when the
new call genuinely differs — and write down how.
