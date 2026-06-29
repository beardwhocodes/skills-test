# Plan 017: Remove the no-op `demo --live` flag so the CLI surface matches behavior

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, your status row will be added to
> `plans/README.md` by the reviewer — do NOT edit `plans/README.md` yourself.
>
> **Drift check (run first)**: This repo is NOT git-initialized, so there is no
> SHA to diff against. Instead, compare the "Current state" excerpts below to
> the live code in `skill_ab_harness.py` at the cited line numbers BEFORE
> editing. If any excerpt no longer matches the file (line numbers may have
> shifted — search by the quoted text), treat it as a STOP condition.

## Status

- **Priority**: P3
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: dx
- **Planned at**: not git-initialized, 2026-06-25

## Why this matters

The `demo` subcommand advertises a `--live` flag (`help="actually run claude
(costs $)"`), but passing it is a **guaranteed error**: `cmd_demo` immediately
raises `SystemExit("demo --live is a follow-up; …")`. The docstring also claims
"--live runs the real pipeline." This is an advertised-but-broken switch: a user
who reads `--help` and runs `skills-test demo --live` gets a crash instead of the
behavior the flag promised. The honest, smallest fix is to **remove** the flag
entirely so the CLI surface matches what `demo` actually does — an offline,
zero-cost render. (A real live-demo path was intentionally deferred when this was
first built; see Maintenance notes. Reviving it is explicitly out of scope here.)

## Current state

Single module under change: `skill_ab_harness.py` (~3715 lines). The `--live`
machinery lives in three spots plus one test.

**1. `cmd_demo` — the function, `skill_ab_harness.py:3397-3418`** (the `live`
parameter, the misleading docstring line, and the dead raise branch):

```python
def cmd_demo(out_dir: Path, live: bool = False) -> None:
    """Offline (default): render the in-code bundled example into an HTML report +
    badge with ZERO claude/git/network/cost. --live runs the real pipeline."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if live:
        raise SystemExit("demo --live is a follow-up; the offline demo spends nothing "
                         "and shows the full report. Run `skills-test run` for a real experiment.")
    results = _demo_results()
    cfg = ExperimentConfig(repo_path=Path("."), base_ref="HEAD",
                           skill_src=Path("demo_skill"),
                           skill_name="write-tests-first", results_dir=out_dir, k=6)
    manifest = experiment_manifest(cfg, seed=0, timestamp=0.0, offline=True)
    html_path = out_dir / "report.html"
    html_path.write_text(build_html_report(results, Preflight(), cfg, manifest))
    summary = summary_dict(results, cfg, manifest)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    verdict = primary_verdict(summary, cfg)
    svg_path = out_dir / "badge.svg"
    if verdict:
        svg_path.write_text(render_badge_svg(cfg.primary_metric, verdict))
    print(f"demo report: {html_path}\ndemo badge:  {svg_path}\n"
          f"open the HTML to see the on/off diff drill-down. No money spent.")
```

**2. argparse wiring inside `main`, `skill_ab_harness.py:3654-3656`** (`main` is
defined at line 3603: `def main(argv: list[str] | None = None) -> int:`):

```python
    pd = sub.add_parser("demo", help="render a bundled example report+badge (offline, free)")
    pd.add_argument("-o", "--out", type=Path, default=Path("skills-test-demo"))
    pd.add_argument("--live", action="store_true", help="actually run claude (costs $)")
```

**3. dispatch inside `main`, `skill_ab_harness.py:3662-3663`**:

```python
    if args.cmd == "demo":
        cmd_demo(args.out, args.live); return 0
```

**4. the demo test, `test_skill_ab_harness.py:499-504`** (passes `live=False`
explicitly — it must stop passing a removed kwarg):

```python
def test_demo_command_writes_offline():
    out = Path(tempfile.mkdtemp())
    h.cmd_demo(out, live=False)
    doc = (out / "report.html").read_text()
    assert doc.startswith("<!doctype html") and "</html>" in doc and "<table" in doc
    assert (out / "badge.svg").exists() and "verified" in (out / "badge.svg").read_text()
```

Conventions that apply here:

- **stdlib-only is a hard rule** — never add a dependency (no numpy/pandas/
  jinja/pytest). Python >= 3.11.
- Comments explain **why**, not what.
- Tests use a custom stdlib runner (NOT pytest) in `test_skill_ab_harness.py`;
  the test module is imported as `h`. argparse on an unknown flag raises
  `SystemExit` (exit code 2) — that is the behavior the new test asserts.
- `README.md:13,47` documents the `demo` command but does NOT mention `--live`;
  `CLAUDE.md` does NOT document `demo --live` (its "live" hits all refer to the
  live `claude` CLI, unrelated). So no doc edits are required — confirm in
  Step 4 rather than assuming.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Tests   | `python3 test_skill_ab_harness.py` | all tests pass, exit 0 |
| Lint    | `uvx ruff check skill_ab_harness.py` | `All checks passed!`, exit 0 |
| Confirm flag gone | `python3 skill_ab_harness.py demo --live` | argparse error: `unrecognized arguments: --live`, exit 2 |
| Confirm demo still works | `python3 skill_ab_harness.py demo -o /tmp/sa-demo-017` | prints `demo report:` / `demo badge:`, exit 0 |

(Run all commands from the repo root: `/Users/copyjosh/Code/skills-test`.)

## Scope

**In scope** (the only files you should modify):
- `skill_ab_harness.py`
- `test_skill_ab_harness.py`

**Out of scope** (do NOT touch):
- `plans/005-demo-zero-setup.md` and any other `plans/00*.md` — those are DONE,
  historical records; do not rewrite history even though 005 introduced this
  flag.
- `plans/README.md` — the reviewer maintains the index; do not edit it.
- `README.md` / `CLAUDE.md` — only edit IF Step 4's grep finds a literal
  `demo --live` reference (it should not). Do not make unrelated doc edits.
- Building an actual live-demo pipeline (the "Alternative" below). This plan is
  REMOVE-only.

## Git workflow

This repo is NOT git-initialized — there is no branch to cut and no commit/PR to
open. Edit the two in-scope files in place. Do not run `git init` or any git
command.

## Steps

### Step 1: Strip `--live` out of `cmd_demo`

In `skill_ab_harness.py`, change the function signature, docstring, and remove
the dead branch. Target shape:

```python
def cmd_demo(out_dir: Path) -> None:
    """Render the in-code bundled example into an HTML report + badge with ZERO
    claude/git/network/cost. Always offline — this command never spends money."""
    out_dir.mkdir(parents=True, exist_ok=True)
    results = _demo_results()
    cfg = ExperimentConfig(repo_path=Path("."), base_ref="HEAD",
```

Concretely: (a) drop the `, live: bool = False` parameter; (b) replace the
`--live runs the real pipeline.` docstring sentence with an offline-only
description; (c) delete the three lines `if live:` / `raise SystemExit(...)`
(both continuation lines of the message). Leave everything from
`results = _demo_results()` onward unchanged.

**Verify**: `grep -n "live" skill_ab_harness.py | grep -i demo` → no output
(no `live` token remains in the demo function/dispatch). And
`grep -n "def cmd_demo" skill_ab_harness.py` → shows `def cmd_demo(out_dir: Path) -> None:`.

### Step 2: Remove the `--live` argparse argument

In `main`, delete this single line (currently `skill_ab_harness.py:3656`):

```python
    pd.add_argument("--live", action="store_true", help="actually run claude (costs $)")
```

Leave the `add_parser("demo", ...)` and `-o/--out` lines intact.

**Verify**: `grep -n 'add_argument("--live"' skill_ab_harness.py` → no output.

### Step 3: Fix the dispatch call

In `main`, change the demo dispatch (currently `skill_ab_harness.py:3663`) from
`cmd_demo(args.out, args.live); return 0` to:

```python
    if args.cmd == "demo":
        cmd_demo(args.out); return 0
```

**Verify**: `grep -n "args.live" skill_ab_harness.py` → no output.

### Step 4: Confirm no docs reference the removed flag

Search the docs for a literal mention of the flag:

```
grep -rn "demo --live\|--live" README.md CLAUDE.md
```

Expected: no matches. If a match IS found, remove only that `demo --live`
phrasing from the doc (keep surrounding prose accurate); do not otherwise rewrite
the docs.

**Verify**: `grep -rn "demo --live" README.md CLAUDE.md` → no output.

### Step 5: Update the existing demo test

In `test_skill_ab_harness.py:501`, change `h.cmd_demo(out, live=False)` to
`h.cmd_demo(out)` (the `live` kwarg no longer exists). Keep the rest of
`test_demo_command_writes_offline` unchanged.

**Verify**: `grep -n "live=False" test_skill_ab_harness.py` → no output.

### Step 6: Add a regression test that `--live` is rejected

Add a small test (next to `test_demo_command_writes_offline`) asserting argparse
no longer accepts the flag. argparse raises `SystemExit` on an unrecognized
argument, so:

```python
def test_demo_live_flag_removed():
    # --live was a no-op that always errored; it must no longer be a known arg.
    try:
        h.main(["demo", "--live", "-o", tempfile.mkdtemp()])
        assert False, "expected SystemExit for unknown --live arg"
    except SystemExit as e:
        assert e.code == 2  # argparse "unrecognized arguments" exit code
```

Note: `h.main` is `skill_ab_harness.main(argv)`; argparse prints an error to
stderr during the test — that is expected and harmless. Match the import alias
`h` already used at the top of the test file.

**Verify**: `python3 test_skill_ab_harness.py` → all tests pass (now including
`test_demo_live_flag_removed`).

## Test plan

- **Adjust** `test_demo_command_writes_offline` (Step 5): drop the `live=False`
  kwarg so the test compiles against the new signature; it still proves the
  offline render writes a valid HTML report + a `verified` badge (happy path).
- **Add** `test_demo_live_flag_removed` (Step 6): the regression for this plan —
  proves `--live` is no longer a recognized argument and the CLI exits 2 instead
  of silently accepting then crashing. Model it structurally after the adjacent
  `test_demo_command_writes_offline` (same `tempfile` + `h.` conventions).
- Verification: `python3 test_skill_ab_harness.py` → all tests pass, including
  the one new test (53 existing + 1 new = 54).

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `python3 test_skill_ab_harness.py` exits 0; `test_demo_live_flag_removed`
      exists and passes; `test_demo_command_writes_offline` still passes.
- [ ] `uvx ruff check skill_ab_harness.py` prints `All checks passed!` (exit 0);
      no line exceeds 100 columns.
- [ ] `grep -n "args.live" skill_ab_harness.py` returns no matches.
- [ ] `grep -n 'add_argument("--live"' skill_ab_harness.py` returns no matches.
- [ ] `grep -n "def cmd_demo" skill_ab_harness.py` shows
      `def cmd_demo(out_dir: Path) -> None:` (no `live` parameter).
- [ ] `python3 skill_ab_harness.py demo --live` exits non-zero with an
      `unrecognized arguments: --live` message.
- [ ] `python3 skill_ab_harness.py demo -o /tmp/sa-demo-017` exits 0 and prints
      `demo report:`.
- [ ] No files outside `skill_ab_harness.py` and `test_skill_ab_harness.py` were
      modified (docs only if Step 4 found a match — it should not have).

## STOP conditions

Stop and report back (do not improvise) if:

- The "Current state" excerpts don't match the live code at the cited lines —
  the codebase has drifted; the `--live` machinery may have already been changed
  or removed.
- Step 4's grep finds `demo --live` documented somewhere unexpected (a plan file,
  a docstring elsewhere) — report it rather than editing out-of-scope files.
- A verification command fails twice after a reasonable fix attempt.
- You conclude a real live demo is actually wanted (see Maintenance notes) — that
  is a different, larger change; report instead of building it under this plan.

## Maintenance notes

For whoever owns this next:

- This intentionally **reverses a deferral**: `plans/005-demo-zero-setup.md`
  shipped `--live` as a stubbed follow-up that always raised. We are deleting the
  stub because an advertised-but-broken flag is worse than no flag. If a real
  live demo is later wanted, it is a fresh feature — build a throwaway git repo +
  a trivial skill, run `run_experiment` at k=1, then render; re-introduce a flag
  only once that path actually works end to end. Do not re-add a flag that
  errors.
- A reviewer should confirm only the four call sites changed (function def,
  docstring, argparse line, dispatch) plus the two test edits, and that `demo`'s
  offline output is byte-for-byte unchanged (the render path was not touched).
- Your status row for plan 017 will be added to `plans/README.md` by the
  reviewer — do not edit that index yourself.
