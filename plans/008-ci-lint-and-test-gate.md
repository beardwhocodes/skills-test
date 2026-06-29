# Plan 008: Add a free CI lint+test gate and clear the standing ruff E702 errors

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, your status row will be added to
> `plans/README.md` by the reviewer — do NOT edit `plans/README.md` yourself.
>
> **Drift check (run first)**: this repo is NOT git-initialized, so there is no
> SHA to diff against. Instead, compare the "Current state" excerpts below to
> the live code before editing. If any excerpt does not match the file
> verbatim (line numbers may shift slightly — match on content, not line
> number), treat it as a STOP condition and report.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: dx
- **Planned at**: not git-initialized, 2026-06-25

## Why this matters

The repo ships a single GitHub Actions workflow (`.github/workflows/skills-test.yml`)
whose only job runs the **paid** `python skills_test.py ci` command (it spends
real money calling `claude -p` and needs `ANTHROPIC_API_KEY`). It is gated behind a
label / `workflow_dispatch`, so nothing runs on an ordinary push or PR. That means
the cheap, deterministic guards — `ruff` lint and the stdlib test suite — never run
in CI. A regression that breaks a test or violates the 100-column lint rule can land
unnoticed. This plan adds a **free, fast, secret-less** CI gate that lints and tests
on every push/PR, and clears the lint errors currently blocking that gate from going
green. After this lands, every push gets immediate lint+test feedback at zero API
cost, and the lint baseline is clean.

## Current state

Files involved:

- `skills_test.py` — the single ~3715-line module under test. Has **4** ruff
  `E702` (multiple-statements-on-one-line / semicolon) errors.
- `test_skills_test.py` — the stdlib test runner (53 tests). Has **9** ruff
  `E702` errors.
- `.github/workflows/skills-test.yml` — the existing PAID A/B gate. Leave it untouched.
- `pyproject.toml` — already configures `[tool.ruff]` with `line-length = 100`, so a
  plain `ruff check <files>` picks up the right line length automatically. No new
  config needed.

### IMPORTANT drift / scope note (read this)

The original framing of this task assumed **4** E702 errors (only in
`skills_test.py`). Recon found that `test_skills_test.py` has **9 more**
E702 errors. Because the new CI gate runs `ruff check skills_test.py
test_skills_test.py` (both files), leaving the test file's 9 errors would make
the new gate **red on its very first run**, defeating the purpose. Therefore this
plan fixes **all 13** E702 sites (4 in the module + 9 in the test file). All are
pure formatting changes — splitting a semicolon-joined line into two lines — with
**zero behavior change**.

### The 4 E702 sites in `skills_test.py`

Inside `cluster_permutation_p` (the permutation loop), `skills_test.py:1158-1159`:

```python
            son += sum(a); non += len(a)
            soff += sum(b); noff += len(b)
```
(These two lines are indented 12 spaces, inside `for ov, fv, k in counts:`.)

Inside `main()`'s command dispatch, `skills_test.py:3660-3663`:

```python
    if args.cmd == "init":
        cmd_init(args.config); return 0
    if args.cmd == "demo":
        cmd_demo(args.out, args.live); return 0
```
(The two offending lines are indented 8 spaces, inside their `if` blocks.)

### The 9 E702 sites in `test_skills_test.py`

`test_skills_test.py:292-294` (indented 4 spaces, in `test_judge_report_coverage_line`):

```python
    on_with = _rr(Arm.SKILL_ON, True, scores={}); on_with.diff = "d"
    on_without = _rr(Arm.SKILL_ON, True, scores={}); on_without.diff = None
    off_with = _rr(Arm.SKILL_OFF, None, scores={}); off_with.diff = "d"
```

`test_skills_test.py:338-339` (indented 12 spaces, in a nested `for`):

```python
            r = _rr(arm, arm is Arm.SKILL_ON, scores={"tests_pass": v}); r.task_id = t
            r.diff = f"diff {t} {arm.value}\n+x"; r.cost_usd = 0.05
```

`test_skills_test.py:420` (indented 4 spaces, in `test_runresult_from_dict_round_trips`):

```python
    r = _rr(Arm.SKILL_ON, True, scores={"tests_pass": 1.0}); r.diff = "diff --git a b\n+x"
```

`test_skills_test.py:617` and `:619` (indented 4 spaces):

```python
    npm = Path(tempfile.mkdtemp()); (npm / "package.json").write_text("{}")
```
```python
    go = Path(tempfile.mkdtemp()); (go / "go.mod").write_text("module x")
```

`test_skills_test.py:718` (indented 8 spaces, in a `for`):

```python
        r = _rr(arm, arm is Arm.SKILL_ON, scores={"tests_pass": v}); r.task_id = "only"
```

### The existing PAID workflow (DO NOT MODIFY) — `.github/workflows/skills-test.yml`

For reference, so you can see what NOT to duplicate. It runs only on
`workflow_dispatch` and a `skills-test` PR label, installs the Claude CLI, and runs the
paid `ci` command with `ANTHROPIC_API_KEY`:

```yaml
name: skills-test
on:
  workflow_dispatch:
  pull_request:
    types: [labeled]        # add the "skills-test" label to trigger
```

The new workflow you add is a **separate file** and must NOT touch this one.

### Conventions that apply

- STDLIB-ONLY is a hard rule for the Python code — never add a dependency. (Not
  relevant to the formatting edits here, but do not "improve" anything else.)
- Comments explain WHY, not what.
- The edits in this plan are mechanical: one semicolon-joined statement → two lines,
  preserving the exact indentation of the original line. No logic changes.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Lint the module | `uvx ruff check skills_test.py` | `All checks passed!` (0 errors) |
| Lint both files (the CI command) | `uvx ruff check skills_test.py test_skills_test.py` | `All checks passed!` (0 errors) |
| Run tests | `python3 test_skills_test.py` | last line `53 passed`, exit 0 |
| Count E702 before fix | `uvx ruff check skills_test.py test_skills_test.py --output-format concise` | 13 lines, all `E702` |

`uvx` runs ruff without installing it. If `uvx` is unavailable in your environment,
`pipx run ruff check ...` or `pip install ruff && ruff check ...` are equivalent;
the `pyproject.toml` `line-length = 100` is honored by all of them.

## Scope

**In scope** (the only files you may modify or create):
- `skills_test.py` — fix the 4 E702 sites.
- `test_skills_test.py` — fix the 9 E702 sites.
- `.github/workflows/lint-test.yml` — **create** this new workflow.

**Out of scope** (do NOT touch):
- `.github/workflows/skills-test.yml` — the paid A/B gate; leave it exactly as-is.
- `plans/README.md` — the reviewer maintains the index; do not edit it.
- `plans/001-*.md` … `plans/007-*.md` — other plans; do not touch.
- Any other line of `skills_test.py` / `test_skills_test.py` beyond the
  13 E702 sites. Do not reformat, rename, or "tidy" anything else.

## Git workflow

This repo is **not** git-initialized — there is no branch to create and no commit to
make. Edit the files in place. Do not run `git init`, `git add`, or `git commit`
unless the operator explicitly asks.

## Steps

### Step 1: Confirm the lint baseline

Run the count command and confirm you see exactly 13 E702 errors across the two
files (4 in `skills_test.py`, 9 in `test_skills_test.py`).

**Verify**: `uvx ruff check skills_test.py test_skills_test.py --output-format concise`
→ exactly 13 lines, every one containing `E702`. If the count differs, STOP (drift).

### Step 2: Fix the 4 E702 sites in `skills_test.py`

Make these four exact replacements. Each splits one semicolon-joined line into two,
keeping the original indentation.

In `cluster_permutation_p`, replace:
```python
            son += sum(a); non += len(a)
            soff += sum(b); noff += len(b)
```
with:
```python
            son += sum(a)
            non += len(a)
            soff += sum(b)
            noff += len(b)
```

In `main()`, replace:
```python
        cmd_init(args.config); return 0
```
with:
```python
        cmd_init(args.config)
        return 0
```

And replace:
```python
        cmd_demo(args.out, args.live); return 0
```
with:
```python
        cmd_demo(args.out, args.live)
        return 0
```

**Verify**: `uvx ruff check skills_test.py` → `All checks passed!` (0 errors).

### Step 3: Fix the 9 E702 sites in `test_skills_test.py`

Make these replacements (each splits the semicolon-joined line, preserving
indentation). Watch the indentation level noted for each — it varies (4, 8, 12).

Replace (4-space indent):
```python
    on_with = _rr(Arm.SKILL_ON, True, scores={}); on_with.diff = "d"
    on_without = _rr(Arm.SKILL_ON, True, scores={}); on_without.diff = None
    off_with = _rr(Arm.SKILL_OFF, None, scores={}); off_with.diff = "d"
```
with:
```python
    on_with = _rr(Arm.SKILL_ON, True, scores={})
    on_with.diff = "d"
    on_without = _rr(Arm.SKILL_ON, True, scores={})
    on_without.diff = None
    off_with = _rr(Arm.SKILL_OFF, None, scores={})
    off_with.diff = "d"
```

Replace (12-space indent):
```python
            r = _rr(arm, arm is Arm.SKILL_ON, scores={"tests_pass": v}); r.task_id = t
            r.diff = f"diff {t} {arm.value}\n+x"; r.cost_usd = 0.05
```
with:
```python
            r = _rr(arm, arm is Arm.SKILL_ON, scores={"tests_pass": v})
            r.task_id = t
            r.diff = f"diff {t} {arm.value}\n+x"
            r.cost_usd = 0.05
```

Replace (4-space indent):
```python
    r = _rr(Arm.SKILL_ON, True, scores={"tests_pass": 1.0}); r.diff = "diff --git a b\n+x"
```
with:
```python
    r = _rr(Arm.SKILL_ON, True, scores={"tests_pass": 1.0})
    r.diff = "diff --git a b\n+x"
```

Replace (4-space indent):
```python
    npm = Path(tempfile.mkdtemp()); (npm / "package.json").write_text("{}")
```
with:
```python
    npm = Path(tempfile.mkdtemp())
    (npm / "package.json").write_text("{}")
```

Replace (4-space indent):
```python
    go = Path(tempfile.mkdtemp()); (go / "go.mod").write_text("module x")
```
with:
```python
    go = Path(tempfile.mkdtemp())
    (go / "go.mod").write_text("module x")
```

Replace (8-space indent):
```python
        r = _rr(arm, arm is Arm.SKILL_ON, scores={"tests_pass": v}); r.task_id = "only"
```
with:
```python
        r = _rr(arm, arm is Arm.SKILL_ON, scores={"tests_pass": v})
        r.task_id = "only"
```

**Verify**: `uvx ruff check skills_test.py test_skills_test.py` →
`All checks passed!` (0 errors).

### Step 4: Confirm tests still pass after the formatting edits

The edits are pure formatting, so all 53 tests must still pass.

**Verify**: `python3 test_skills_test.py` → last line is `53 passed`, exit 0.

### Step 5: Create the new free CI workflow

Create the file `.github/workflows/lint-test.yml` with exactly this content:

```yaml
# Free, fast lint + test gate. Runs on every push and pull_request.
# No secrets, no Claude CLI, no network beyond installing ruff — so it costs
# nothing and gives immediate feedback. The PAID A/B regression gate lives in
# skills-test.yml and stays opt-in (label / workflow_dispatch); this complements it.
name: lint-test
on:
  push:
  pull_request:

jobs:
  lint-test:
    runs-on: ubuntu-latest
    timeout-minutes: 5
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install ruff
        run: pip install ruff

      - name: Lint (line-length 100 from pyproject.toml)
        run: ruff check skills_test.py test_skills_test.py

      - name: Tests (stdlib runner, no claude/git/network)
        run: python test_skills_test.py
```

**Verify** the file is valid YAML:
`python3 -c "import sys; print('NOTE: PyYAML may be absent') if False else None"` —
actually use the stdlib-safe check below, since PyYAML is not guaranteed:
`python3 - <<'PY'
import pathlib
p = pathlib.Path(".github/workflows/lint-test.yml")
t = p.read_text()
assert "name: lint-test" in t, "missing workflow name"
assert "ruff check skills_test.py test_skills_test.py" in t, "missing lint step"
assert "python test_skills_test.py" in t, "missing test step"
assert "secrets" not in t, "workflow must not reference secrets"
assert "ANTHROPIC_API_KEY" not in t, "workflow must not use the API key"
print("workflow OK")
PY`
→ prints `workflow OK`. If PyYAML happens to be installed you may additionally run
`python3 -c "import yaml,pathlib; yaml.safe_load(pathlib.Path('.github/workflows/lint-test.yml').read_text()); print('valid yaml')"`
→ prints `valid yaml` (skip this if PyYAML is not installed — it is not a project
dependency).

## Test plan

- **No new Python tests are required.** This plan only reformats existing lines
  (semicolon splits, no behavior change) and adds a CI YAML file. Writing a test
  for either would test framework/formatter behavior, which the conventions say to
  avoid.
- Regression guard is the existing suite: `python3 test_skills_test.py` must
  still report `53 passed` after the edits (Step 4). Because the edits touch lines
  inside `cluster_permutation_p` and `main()`'s dispatch, the existing tests that
  exercise the permutation p-value and the CLI dispatch already cover that the
  behavior is unchanged.
- Verification: `python3 test_skills_test.py` → `53 passed`.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `uvx ruff check skills_test.py` → `All checks passed!` (0 errors).
- [ ] `uvx ruff check skills_test.py test_skills_test.py` →
      `All checks passed!` (0 errors). (This is the exact command the new CI runs.)
- [ ] `python3 test_skills_test.py` → `53 passed`, exit 0.
- [ ] `.github/workflows/lint-test.yml` exists, references no secrets and no
      `ANTHROPIC_API_KEY`, runs both the `ruff check` and `python
      test_skills_test.py` steps, and triggers on `push` and `pull_request`.
- [ ] `.github/workflows/skills-test.yml` is byte-for-byte unchanged.
- [ ] No files outside the in-scope list were modified.
- [ ] `plans/README.md` was NOT edited (the reviewer maintains it).

## STOP conditions

Stop and report back (do not improvise) if:

- Step 1 does not show exactly 13 E702 errors (4 in the module, 9 in the test file),
  or the "Current state" excerpts do not match the live files — the codebase has
  drifted since this plan was written.
- A ruff verification still reports errors after your edits, and the remaining
  errors are NOT the E702 sites listed here (a new lint rule or unrelated error
  surfaced — report it rather than fixing unlisted code).
- `python3 test_skills_test.py` does not report `53 passed` after the edits —
  a "pure formatting" change unexpectedly altered behavior; do not patch tests to
  make them pass.
- Fixing any site appears to require touching an out-of-scope file.
- `uvx`, `pipx`, and `pip` are all unavailable so you cannot run ruff at all —
  report the environment gap rather than guessing.

## Maintenance notes

For whoever owns this next:

- The new `lint-test.yml` lints **both** `skills_test.py` and
  `test_skills_test.py`. If a future change adds a new top-level Python file,
  add it to the `ruff check` line so it is covered too.
- The lint step relies on `pyproject.toml`'s `[tool.ruff] line-length = 100`. If
  that config moves or changes, the CI line-length follows it automatically — keep
  the config as the single source of truth (don't hard-code `--line-length` in the
  workflow).
- The test step uses the project's custom stdlib runner (`python
  test_skills_test.py`), NOT pytest — do not "modernize" it to `pytest`; the
  STDLIB-ONLY rule forbids adding pytest as a dependency.
- A reviewer should confirm the new workflow carries no secrets and adds no paid
  `claude` call — its whole value is being free and fast. The paid gate stays in
  `skills-test.yml`, opt-in behind a label / `workflow_dispatch`.
- Deferred out of this plan: enabling ruff auto-fix in CI (`ruff check --fix`) or
  adding `ruff format`. Intentionally omitted — this plan only establishes the gate
  and clears the existing baseline; format/auto-fix policy is a separate decision.
