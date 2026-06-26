# Plan 012: Resume must not double-count a retried-failed cell

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. Your status row in `plans/README.md` is maintained
> by the reviewer who dispatched you — do NOT edit `plans/README.md`.
>
> **Drift check (run first)**: this repo is NOT git-initialized, so there is no
> SHA to diff against. Before editing, compare the "Current state" excerpts
> below to the live code in `skill_ab_harness.py` at the cited line numbers. If
> the live code no longer matches an excerpt (line numbers may have shifted —
> match on the code text, not the number), treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: MED
- **Depends on**: none
- **Category**: bug
- **Planned at**: not git-initialized, 2026-06-25

## Why this matters

On `--resume`, a cell (one `(task_id, arm, run_index)` triple) that previously
**failed** is retried, but the harness then returns `prior + fresh` with **no
dedup**. The failed attempt lives in `prior`; its retry lives in `fresh`; the
same cell now appears **twice** in the analysis list. That inflates every raw
denominator computed by counting rows — e.g. the validity line `tot = sum(1 for
r in results if r.arm is arm)` in the report, and the `total runs` / `nRuns`
counts in the markdown and HTML reports. A run that succeeded on the second try
gets reported as "1/2 valid" instead of "1/1"; a perpetually-failing cell shows
"0/N valid" where N grows every resume.

Separately, `results.jsonl` is **append-only on resume** (only a fresh run
truncates it), so a cell that keeps failing across many resumes grows the file
without bound.

**The inferential statistics are NOT affected** and this plan must not pretend
otherwise: a retried-and-duplicated record is always non-`itt_valid` (a failed
attempt has `agent_ok=False`; a contaminated attempt has `contaminated=True`),
and `estimate_diff` only ever sees rows that pass the `itt_valid`/`pp_valid`
filter inside `_grouped` (see Current state). So CIs and p-values are unchanged.
The bug is confined to the **audit/denominator counts and unbounded disk
growth**. Fixing it makes the reported "valid/total", activation, and "runs"
counts truthful and bounds the JSONL file.

## Current state

Single module under change: `skill_ab_harness.py` (~3715 lines, stdlib-only,
Python >=3.11). Tests: `test_skill_ab_harness.py` (custom stdlib runner, NOT
pytest). The repo is not git-initialized; edit in place.

### The bug — `run_experiment` resume + return (`skill_ab_harness.py:1018-1061`)

```python
    prior: list[RunResult] = []
    done: set[tuple[str, str, int]] = set()
    if resume and results_path.exists():
        _check_resume_compatible(manifest_path, manifest)
        prior = load_results(results_path)
        # Only SUCCESSFUL cells count as done -- failed/timed-out cells are kept on
        # disk for the audit trail but must be retried, not silently skipped.
        done = {(r.task_id, r.arm.value, r.run_index) for r in prior if r.agent_ok}
    else:
        results_path.write_text("")          # truncate prior run
```

Jobs are built only for cells **not** in `done`, so a failed cell re-runs:

```python
    jobs = [
        run_and_persist(task, arm, i)
        for task in tasks
        for arm in experiment_arms(cfg)
        for i in range(cfg.k)
        if (task.id, arm.value, i) not in done
    ]
    raw = await asyncio.gather(*jobs, return_exceptions=True)
    fresh = [r for r in raw if isinstance(r, RunResult) and r.error != _SKIPPED_ERR]
    return prior + fresh, pf
```

`prior` contains the failed attempt; `fresh` contains its retry; `prior + fresh`
returns both. There is no dedup. (Persistence inside `run_and_persist` appends
to `results_path` — `skill_ab_harness.py:1046` `with results_path.open("a")` —
which on resume adds to the existing file rather than replacing it.)

### Where the duplicate inflates a denominator (`skill_ab_harness.py:1269-1272`)

```python
    for arm in experiment_arms(cfg):
        label = arm_label(cfg, arm)
        tot = sum(1 for r in results if r.arm is arm)
        val = sum(1 for r in results if r.arm is arm and r.itt_valid)
        fired, clean = activation_rate(results, arm)
        contam = sum(1 for r in results if r.arm is arm and r.contaminated)
```

`tot` counts every row (duplicate included → inflated). `val` counts only
`itt_valid` rows (the duplicated failed attempt is `itt_valid=False`, so `val`
is correct) → the displayed ratio `val/tot` is wrong. The markdown report also
prints raw `len(results)`:

```python
        f"| total runs: {len(results)}",        # skill_ab_harness.py:1253
```

and the HTML report derives `nRuns` the same way (`var nRuns = (D.runs ||
[]).length;` at `skill_ab_harness.py:2300`, fed by the full results list).

### Why the stats are safe — `_grouped` filters before counting (`skill_ab_harness.py:1088-1093`)

```python
    g: dict[str, list[float]] = {}
    for r in results:
        ok = r.itt_valid if mode == "itt" else r.pp_valid
        if r.arm is arm and ok and metric in r.scores:
            g.setdefault(r.task_id, []).append(r.scores[metric])
    return g
```

`estimate_diff` (`skill_ab_harness.py:1180`) consumes `_grouped`, so a
duplicated non-valid row never reaches the bootstrap/permutation math.

### Validity properties that make a duplicate always non-valid (`skill_ab_harness.py:274-293`)

```python
    @property
    def itt_valid(self) -> bool:
        ...
        if not self.agent_ok:
            return False
        return not self.contaminated
```

A cell is only retried when it is **not** in `done`, and `done` is
`{... if r.agent_ok}`. So the prior record being retried has `agent_ok=False`
(or is contaminated — see note in Step 1) → `itt_valid=False`. The duplicate is
always invalid, which is exactly why the stats are unaffected.

### Persistence helpers you must keep compatible (`skill_ab_harness.py:295-333`, `1726-1736`)

`RunResult.to_dict` serializes one row; `from_dict` rebuilds it and **recomputes**
derived keys (`contaminated`/`itt_valid`/`pp_valid`) rather than reading them:

```python
    @classmethod
    def from_dict(cls, d: dict) -> "RunResult":
        """Rebuild from a results.jsonl line. Derived keys (contaminated /
        itt_valid / pp_valid) are recomputed, not read."""
```

`load_results` tolerates a truncated trailing line (do not break this):

```python
def load_results(path: Path) -> list[RunResult]:
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(RunResult.from_dict(json.loads(line)))
        except (json.JSONDecodeError, KeyError):
            continue   # tolerate a partial trailing line from a killed run (resume)
    return out
```

### Conventions to match

- Comments explain **why**, not what.
- stdlib-only is a hard rule. Do NOT add any dependency (no numpy/pandas/pytest).
- Line length < 100 columns (`uvx ruff check`, configured line-length 100).
- Tests live in `test_skill_ab_harness.py` and are discovered by a custom runner
  that calls every top-level `test_*` function **with no arguments** (see
  `_run_all` at `test_skill_ab_harness.py:728-734`). A new test must be callable
  as `test_x()`. Build `RunResult`s with the existing helper `_rr(arm, activated,
  agent_ok=True, **kw)` at `test_skill_ab_harness.py:41-45` (it sets `task_id="t"`,
  `run_index=0`, and `arm_skill_name` automatically) — pass `task_id=`,
  `run_index=`, `agent_ok=` as kwargs to vary the cell key.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Tests   | `python3 test_skill_ab_harness.py` | ends with `54 passed` (53 today + your 1 new); exit 0 |
| Lint    | `uvx ruff check skill_ab_harness.py` | `All checks passed!`; exit 0 |
| Lint test file | `uvx ruff check test_skill_ab_harness.py` | `All checks passed!`; exit 0 |

(Run from the repo root `/Users/copyjosh/Code/skills-test`. The test runner is
NOT pytest; do not invoke `pytest`.)

## Scope

**In scope** (the only files you should modify):
- `skill_ab_harness.py` — add `_dedupe_runs`; call it at the end of
  `run_experiment`; optionally rewrite `results.jsonl`.
- `test_skill_ab_harness.py` — add one unit test for `_dedupe_runs`.

**Out of scope** (do NOT touch, even though they look related):
- `plans/README.md` — the reviewer maintains the index.
- `plans/001`–`plans/006` — DONE; do not touch.
- The `done`/`jobs` logic and the `_check_resume_compatible` guard — the retry
  behavior is correct; only the **return value** double-counts. Do not change
  which cells re-run.
- `estimate_diff` / `_grouped` / any statistics — they are already correct
  (they filter on `itt_valid`). Changing them is out of scope and would risk the
  pre-registered primary endpoint.
- The per-result append in `run_and_persist` (`results_path.open("a")`) — leave
  the incremental append as-is; the optional rewrite in Step 2 happens once at
  the end, after `gather`.

## Git workflow

The repo is not git-initialized. Edit the files in place. Do not init git, do
not branch, do not commit, do not open a PR.

## Steps

### Step 1: Add a pure `_dedupe_runs(prior, fresh)` helper and use it in the return

Add a module-level function near `run_experiment` (e.g. directly above it, after
the `_SKIPPED_ERR = ...` line at `skill_ab_harness.py:1002`). Target shape:

```python
def _dedupe_runs(prior: list[RunResult], fresh: list[RunResult]) -> list[RunResult]:
    """Collapse to the LAST record per (task_id, arm, run_index). On resume a
    failed/contaminated cell is retried, so the SAME cell can appear in both
    `prior` (the superseded attempt) and `fresh` (the retry); returning both
    would double-count it in raw denominators. `fresh` wins over `prior`; a cell
    seen only once passes through unchanged. Order is stable: a cell keeps the
    position where it was first seen (prior order, then fresh-only cells)."""
    by_cell: dict[tuple[str, str, int], RunResult] = {}
    for r in (*prior, *fresh):     # prior first; a later fresh retry overwrites it
        by_cell[(r.task_id, r.arm.value, r.run_index)] = r
    return list(by_cell.values())
```

(`dict` preserves first-insertion order on overwrite, so a retried cell keeps
its prior position but holds the fresh value — that is the "stable order"
guarantee the test checks.)

Then replace the return in `run_experiment` (`skill_ab_harness.py:1061`):

```python
    return prior + fresh, pf
```

with:

```python
    return _dedupe_runs(prior, fresh), pf
```

**Note for the executor**: the docstring says "failed/contaminated" because a
retried cell is one that was not in `done` (`done` requires `agent_ok`), so its
prior record either failed (`agent_ok=False`) or — if it ran clean but was
contaminated — was still re-run; either way the superseded prior record is
non-`itt_valid`. You do not need to special-case this; last-write-wins keyed on
the cell triple is correct regardless of why the cell was retried.

**Verify**: `python3 test_skill_ab_harness.py` still ends with `53 passed`
(no behavior change yet for existing tests) and `uvx ruff check
skill_ab_harness.py` prints `All checks passed!`.

### Step 2 (recommended): Rewrite `results.jsonl` once at the end so disk matches memory

So a perpetually-failing cell stops growing the file on every resume, rewrite
the JSONL from the deduped set after `gather` completes. Change the tail of
`run_experiment` to compute the deduped list once, rewrite the file, and return
it:

```python
    deduped = _dedupe_runs(prior, fresh)
    # Rewrite results.jsonl from the deduped set so disk matches the in-memory
    # analysis list and a repeatedly-retried cell stops growing the file. Safe
    # after gather: no concurrent writers remain. load_results/from_dict
    # recompute derived keys, so a rewritten line round-trips identically.
    with results_path.open("w") as f:
        for r in deduped:
            f.write(json.dumps(r.to_dict()) + "\n")
    return deduped, pf
```

This replaces the `return _dedupe_runs(prior, fresh), pf` from Step 1 (call the
helper once, not twice). Keep this write **outside** any `async with write_lock`
block — it runs after `asyncio.gather`, single-threaded.

**Tradeoff to record (not a blocker)**: rewriting drops the superseded failed
attempt's row from `results.jsonl`. That row's deeper audit trail (events, diff,
stderr) is already dumped to `results_dir/artifacts/<label>/` for failed/invalid
runs, so the rewrite only removes the redundant summary line, not the audit
evidence. If you judge that losing the superseded line from `results.jsonl` is
unacceptable for this repo, implement Step 1 only and skip Step 2 — Step 1 alone
fixes the denominators; Step 2 additionally bounds disk growth.

**Verify**: `python3 test_skill_ab_harness.py` still passes; `uvx ruff check
skill_ab_harness.py` clean.

### Step 3: Add the unit test for `_dedupe_runs`

Add one top-level `test_*` function to `test_skill_ab_harness.py` (place it near
the other persistence/round-trip tests, e.g. after
`test_runresult_from_dict_round_trips` at `test_skill_ab_harness.py:419-423`).
It must be callable with no arguments. Cover all four required cases:

```python
def test_dedupe_runs_collapses_retried_cells():
    # failed-then-succeeded: the success wins, one row out.
    failed = _rr(Arm.SKILL_ON, None, agent_ok=False, run_index=0)
    retry_ok = _rr(Arm.SKILL_ON, True, agent_ok=True, run_index=0)
    out = h._dedupe_runs([failed], [retry_ok])
    assert len(out) == 1 and out[0].agent_ok is True

    # failed-then-failed: still exactly one row.
    f1 = _rr(Arm.SKILL_ON, None, agent_ok=False, run_index=1)
    f2 = _rr(Arm.SKILL_ON, None, agent_ok=False, run_index=1)
    assert len(h._dedupe_runs([f1], [f2])) == 1

    # an untouched successful cell (different key, only in prior) passes through.
    done_ok = _rr(Arm.SKILL_OFF, None, agent_ok=True, run_index=2)
    out2 = h._dedupe_runs([done_ok, failed], [retry_ok])
    assert len(out2) == 2
    keys = [(r.task_id, r.arm.value, r.run_index) for r in out2]
    # stable order: prior cells keep their first-seen position.
    assert keys == [("t", Arm.SKILL_OFF.value, 2), ("t", Arm.SKILL_ON.value, 0)]
```

(`_rr` already sets `task_id="t"`; vary `run_index`/`arm`/`agent_ok` to control
the cell key. `arm_skill_name` is set by `_rr` based on the arm — fine here.)

**Verify**: `python3 test_skill_ab_harness.py` ends with `54 passed`; the line
`ok  test_dedupe_runs_collapses_retried_cells` appears in the output.

## Test plan

- New test: `test_dedupe_runs_collapses_retried_cells` in
  `test_skill_ab_harness.py`, covering (1) failed→succeeded collapses to the
  success, (2) failed→failed collapses to one, (3) an untouched successful cell
  passes through, (4) stable ordering.
- Structural pattern to model after: `test_runresult_from_dict_round_trips`
  (`test_skill_ab_harness.py:419-423`) for how it builds `RunResult`s via `_rr`
  and asserts on plain attributes — no async, no `claude`, no `git`.
- Do NOT write an async/`run_experiment` integration test — it would require
  `claude`/`git` and the custom runner cannot drive coroutines. The pure helper
  test is sufficient because the fix is a single pure transform applied at the
  return.
- Verification: `python3 test_skill_ab_harness.py` → `54 passed` (53 existing + 1
  new); `uvx ruff check skill_ab_harness.py` and `uvx ruff check
  test_skill_ab_harness.py` → `All checks passed!`.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `python3 test_skill_ab_harness.py` exits 0 and prints `54 passed`, including
      the new `test_dedupe_runs_collapses_retried_cells`.
- [ ] `uvx ruff check skill_ab_harness.py` exits 0 (`All checks passed!`).
- [ ] `uvx ruff check test_skill_ab_harness.py` exits 0.
- [ ] `grep -n "return prior + fresh" skill_ab_harness.py` returns NO matches
      (the un-deduped return is gone).
- [ ] `grep -n "def _dedupe_runs" skill_ab_harness.py` returns exactly one match.
- [ ] No files outside `skill_ab_harness.py` and `test_skill_ab_harness.py` are
      modified (in particular `plans/README.md` is untouched).

## STOP conditions

Stop and report back (do not improvise) if:

- The code at `skill_ab_harness.py:1018-1061` no longer matches the "Current
  state" excerpts (the resume block or the `return prior + fresh` line has
  drifted) — the dedup point may have moved.
- You find a code path where two `RunResult`s legitimately share the same
  `(task_id, arm.value, run_index)` yet must BOTH be kept (e.g. an intended
  re-run that should appear twice in the analysis). The whole fix assumes that
  triple is the unique cell identity; if that assumption is false,
  `_dedupe_runs` would hide a real record — STOP and report instead of merging.
- A verification command fails twice after a reasonable fix attempt.
- Implementing the fix appears to require touching `_grouped`, `estimate_diff`,
  the `done`/`jobs` construction, or any out-of-scope file.

## Maintenance notes

- For the reviewer: confirm (1) `estimate_diff` is genuinely untouched and CIs
  did not move, (2) the dedup key matches the `done` key
  `(r.task_id, r.arm.value, r.run_index)` exactly — they must stay in sync, and
  (3) if Step 2 was taken, the rewrite happens after `gather` (no concurrent
  writers) and `load_results` still tolerates a truncated trailing line.
- If a future change ever makes a cell legitimately run more than once within a
  single experiment (e.g. per-cell retries that should all be retained), this
  last-write-wins collapse must be revisited — the cell triple would no longer be
  a unique identity.
- Your status row will be added to `plans/README.md` by the reviewer; do not edit
  that file.
