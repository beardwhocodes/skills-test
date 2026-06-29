# Plan 018: Bundle of small, independent hardening + polish fixes

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. The four steps are INDEPENDENT — you may do any subset, in any
> order, and each one leaves the repo in a working state on its own. If anything
> in the "STOP conditions" section occurs, stop and report — do not improvise.
> A reviewer maintains `plans/README.md`; do NOT edit the index yourself.
>
> **Drift check (run first)**: this repo is NOT git-initialized, so there is no
> SHA to diff against. Before editing, compare each "Current state" excerpt
> below against the live code at the cited `file:line`. If the live code no
> longer matches an excerpt, treat that step's edit as a STOP condition (the
> file has drifted since this plan was written) and report it.

## Status

- **Priority**: P3
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: tech-debt | security | docs
- **Planned at**: not git-initialized, 2026-06-25
- **Issue**: —

## Why this matters

Four unrelated, low-risk loose ends. (1) A `task.id` from an untrusted remote
`skillab.toml` is interpolated raw into filesystem paths and a git branch name —
a `../`-laden id can make the harness write outside its results/worktree roots.
(2) The HTML report's strip chart draws a bogus "mean" marker at value 0 with a
`—` label for an arm that has only invalid runs, contradicting the bar chart,
which correctly drops that arm — the two charts disagree on the same data. (3)
Several docs tell the reader to run `python …`, which does not exist on
python3-only machines. (4) `_pooled_mean_diff` is dead production code kept alive
only by a self-referential test. None of these are urgent; together they remove
a security foot-gun, a visual contradiction, a copy-paste failure, and clutter.

## Current state

Single module under change: `/Users/copyjosh/Code/skills-test/skill_ab_harness.py`
(~3715 lines). Tests: `/Users/copyjosh/Code/skills-test/test_skill_ab_harness.py`
(53 tests, custom stdlib runner — NOT pytest). Docs:
`/Users/copyjosh/Code/skills-test/README.md`,
`/Users/copyjosh/Code/skills-test/CLAUDE.md`.

**Hard rules for this repo (do not violate):**

- **STDLIB-ONLY.** Never add a dependency (no numpy/pandas/jinja/pytest).
  Python ≥ 3.11. `re`, `statistics`, `random` are already imported — you will
  not add any import.
- The HTML report's CSS lives in a triple-quoted Python string (`_HTML_STYLE`);
  the report's JS lives in a raw triple-quoted r-string (`_HTML_SCRIPT`) and is
  **ES5 vanilla JS** — `var`/`function` only, NO template literals, NO backticks,
  NO arrow functions. Keep every line **< 100 columns**.
- Comments explain WHY, not what.

**Pre-existing lint baseline (NOT yours to fix):** `uvx ruff check
skill_ab_harness.py` already reports **exactly 4 `E702` errors** (semicolon
statements) at lines 1158, 1159, 3661, 3663. These predate this plan and are
out of scope. Your job is to add **zero new** errors. (Note: Step 4 deletes
~5 lines and will shift those line numbers up by ~5 — the count stays 4.)

### Step 1 facts — `task.id` flows into filesystem + branch paths

`task.id` is built into a per-run `label`, which becomes a worktree directory, a
git branch, and an artifacts directory:

`skill_ab_harness.py:850` (inside `execute_run`):
```python
    label = f"{task.id}-{arm.value}-{idx}"
```
`skill_ab_harness.py:413-414` (inside `Worktree.__init__`):
```python
        self.path = cfg.worktree_root / label
        self.branch = f"ab/{label}"
```
`skill_ab_harness.py:823` (inside `_dump_artifacts`):
```python
        dest = cfg.results_dir / "artifacts" / label
```
`skill_ab_harness.py:843` (inside `_failed_result`):
```python
        worktree=cfg.worktree_root / f"{task.id}-{arm.value}-{idx}",
```

The untrusted path: `--from-github` → `_clone_from_github` shallow-clones a repo
and calls `load_config(cfg_path)`, whose
`skill_ab_harness.py:3233` builds tasks from the remote TOML:
```python
    tasks = [Task(**t) for t in data.get("task", [])]
```
So a remote `skillab.toml` controls `task.id`. An id like `../evil` (it contains
`/`) makes `cfg.worktree_root / "../evil-on-0"` resolve OUTSIDE `worktree_root`.

`Task` is a frozen dataclass with no validation today —
`skill_ab_harness.py:96-110`:
```python
@dataclass(frozen=True)
class Task:
    """One coding problem thrown at both arms."""
    id: str
    prompt: str
    # Shell commands run inside the worktree.
    # setup_cmd runs ONCE right after checkout (before the agent) to install deps
    # etc. A fresh `git worktree add` has no node_modules / venv / build state, so
    # without this the scorers fail on the clean base and get quarantined.
    setup_cmd: str | None = None
    # Scorers run AFTER the agent finishes. 0 = pass. Stack-agnostic.
    test_cmd: str | None = None
    lint_cmd: str | None = None
    build_cmd: str | None = None
```

**Exemplar to copy** — `ExperimentConfig.__post_init__` already raises
`ValueError` for bad config, `skill_ab_harness.py:170-178`:
```python
    def __post_init__(self):
        # Head-to-head needs BOTH skill_b fields. A half-set pair would otherwise
        # make a degenerate second "control" arm (label collision, control_vs_control).
        if (self.skill_b_src is None) != (self.skill_b_name is None):
            raise ValueError("skill_b_src and skill_b_name must be set together (3-arm "
                             "head-to-head) or both left unset (2-arm skill-on/off).")
        if self.skill_b_name is not None and self.skill_b_name == self.skill_name:
            raise ValueError("skill_b must differ from skill_a (same name -> label/pair "
                             "collision).")
```
And the same charset guard already used for owner/repo in
`resolve_target`, `skill_ab_harness.py:3497-3499`:
```python
        if (not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", owner_repo)
                or ".." in owner_repo or owner.startswith("-") or reponame.startswith("-")):
            raise SystemExit(f"unsafe owner/repo in URL: {owner_repo!r}")
```
All existing Task ids are already safe under this rule: `"task"`,
`"add-pagination"`, `"add-email-validation"`, `"fix-null-deref"`, and the test
ids `"a"`, `"b"` — so adding the guard breaks nothing.

### Step 2 facts — strip-chart null-mean marker

`stripChart` includes an arm if ANY run (valid OR invalid) has a non-null score —
`skill_ab_harness.py:2719-2721`:
```javascript
  function stripChart(m){
    var arms = (D.arms || []).filter(function(a){
      return runsFor(a).some(function(r){ return r.scores[m] != null; }); });
```
But the per-arm mean comes from `armMeans`, which is computed over ITT-valid runs
only and is `null` for an all-invalid arm —
`skill_ab_harness.py:2236-2239`:
```javascript
  function mean(a, m){
    var v = D.armMeans && D.armMeans[a] ? D.armMeans[a][m] : null;
    return v == null ? null : v;
  }
```
So for an arm whose only non-null scores are on invalid runs, `mv` is `null`. JS
coerces `null` to `0` in `y(mv)`, so the marker is drawn at value 0, and
`fmtVal(m, null)` prints `—` (see `fmtVal`, `skill_ab_harness.py:2247-2252`). The
bug site, `skill_ab_harness.py:2742` and `2755-2761`:
```javascript
      var mv = mean(a, m), spacing = Math.min(22, band / (rs.length + 1));
```
```javascript
      var my = y(mv);
      var meanMark = "<line x1='" + (cx - band * 0.32) + "' y1='" + my.toFixed(1)
        + "' x2='" + (cx + band * 0.32) + "' y2='" + my.toFixed(1) + "' stroke='" + c
        + "' stroke-width='2.4'/><text class='gtxt-strong' x='"
        + (cx + band * 0.32 + 4) + "' y='" + (my + 4).toFixed(1) + "' fill='" + c
        + "'>" + fmtVal(m, mv) + "</text>";
```
The bar chart already guards correctly — `barChart`, `skill_ab_harness.py:2605`:
```javascript
    var arms = (D.arms || []).filter(function(a){ return mean(a, m) != null; });
```

### Step 3 facts — `python` vs `python3` in docs

`README.md:13`:
```
python skill_ab_harness.py demo          # or: skills-test demo  (after pip/uvx install)
```
`README.md:110`:
```
`python test_skill_ab_harness.py` — stdlib-only, no `claude`/`git` required.
```
`CLAUDE.md:99`:
```
`python test_skill_ab_harness.py` (stdlib-only; no `claude`/`git` needed) covers
```
`test_skill_ab_harness.py:6` (module docstring):
```
Run: `python -m pytest -q` or `python test_skill_ab_harness.py`.
```

### Step 4 facts — `_pooled_mean_diff` is dead production code

`skill_ab_harness.py:1096-1100`:
```python
def _pooled_mean_diff(on: dict[str, list[float]], off: dict[str, list[float]],
                      shared: list[str]) -> float:
    on_vals = [v for t in shared for v in on[t]]
    off_vals = [v for t in shared for v in off[t]]
    return statistics.fmean(on_vals) - statistics.fmean(off_vals)
```
`grep -rn "_pooled_mean_diff"` over the repo returns exactly TWO lines: the
definition above, and **one self-referential test caller** — there is NO
production caller. The test, `test_skill_ab_harness.py:93-100`:
```python
def test_permutation_p_small_for_strong_effect():
    on = {"a": [1.0] * 8, "b": [1.0] * 8}
    off = {"a": [0.0] * 8, "b": [0.0] * 8}
    point = h._pooled_mean_diff(on, off, ["a", "b"])
    assert point == 1.0
    p = h.cluster_permutation_p(on, off, ["a", "b"], point, 5000, random.Random(1))
    assert p < 0.01
    assert p > 0.0          # +1 smoothing: never exactly zero
```
The helper is used here only to derive `point`, which the test already asserts
equals `1.0` (all-1 minus all-0). So the helper can be removed and `point`
inlined without weakening what the test actually checks (`cluster_permutation_p`
on a strong effect). `statistics` stays imported — it is used elsewhere (e.g.
`cluster_bootstrap_ci`), so removing this helper does NOT create an unused import.

> NOTE / drift from the originating audit: the audit assumed `_pooled_mean_diff`
> had "no callers." That is false — the self-test above calls it. This plan
> handles that by updating the test. If grep ever shows a caller OTHER than this
> one test line, STOP (see STOP conditions).

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Tests   | `python3 test_skill_ab_harness.py` | ends with `53 passed` (or `54 passed` after Step 1 adds a test) |
| Lint    | `uvx ruff check skill_ab_harness.py` | `Found 4 errors.` — all `E702`, all pre-existing (see baseline); **count must not increase** |
| Lint count | `uvx ruff check skill_ab_harness.py 2>&1 \| grep -c E702` | `4` |
| Caller grep | `grep -rn "_pooled_mean_diff" /Users/copyjosh/Code/skills-test` | after Step 4: no matches |

Run all commands from `/Users/copyjosh/Code/skills-test`. Use `python3`, not
`python` (this machine may be python3-only — that is the subject of Step 3).

## Scope

**In scope** (the only files you may modify):
- `/Users/copyjosh/Code/skills-test/skill_ab_harness.py` (Steps 1, 2, 4)
- `/Users/copyjosh/Code/skills-test/test_skill_ab_harness.py` (Steps 1, 4)
- `/Users/copyjosh/Code/skills-test/README.md` (Step 3)
- `/Users/copyjosh/Code/skills-test/CLAUDE.md` (Step 3)

**Out of scope** (do NOT touch):
- The 4 pre-existing `E702` lint errors (lines 1158, 1159, 3661, 3663) — not
  this plan's problem; fixing them is unrelated churn.
- `plans/README.md` — the reviewer maintains the index.
- Any other plan file `plans/0NN-*.md`.
- Adding any third-party dependency — forbidden (stdlib-only).
- The estimand / validity logic, the bootstrap, or any scorer — untouched here.

## Git workflow

This repo is NOT git-initialized — there is no branch to create and no PR to
open. Edit the files in place. Do not run `git init`.

## Steps

### Step 1: Validate `task.id` against a safe charset

Add a `__post_init__` to the `Task` dataclass
(`skill_ab_harness.py:96-110`) that rejects any id containing a path separator
or `..`, mirroring `resolve_target`'s owner/repo guard. Put it immediately after
the field declarations (after `build_cmd: str | None = None`), before the blank
line that precedes `@dataclass(frozen=True)\nclass ExperimentConfig`. Target
shape:

```python
    def __post_init__(self):
        # task.id is interpolated raw into a worktree dir, a git branch (ab/<label>),
        # and an artifacts/<label> dir; a '/' or '..' from an untrusted remote
        # skillab.toml (see _clone_from_github -> load_config) would escape those
        # roots. Same charset rule as resolve_target's owner/repo guard.
        if ".." in self.id or not re.fullmatch(r"[A-Za-z0-9._-]+", self.id):
            raise ValueError(f"unsafe task id {self.id!r}: use only [A-Za-z0-9._-] "
                             f"(no path separators, no '..').")
```

`re` is already imported (`skill_ab_harness.py:69`) — do not add an import.
A `__post_init__` on a frozen dataclass is allowed; it only READS `self.id`.

Then add two tests to `test_skill_ab_harness.py` (top-level functions named
`test_*` — the runner auto-discovers them; model after
`test_skill_a_equals_skill_b_rejected` at `test_skill_ab_harness.py:687-693`,
which uses a `try/except ValueError` shape). Place them anywhere at module level,
e.g. after `test_skill_a_equals_skill_b_rejected`:

```python
def test_task_id_path_traversal_rejected():
    try:
        h.Task(id="../evil", prompt="x")
        assert False, "should reject task id with a path separator"
    except ValueError:
        pass


def test_task_id_normal_accepted():
    # The exact ids the harness itself uses must still pass.
    for good in ("task", "add-email-validation", "fix-null-deref", "v1.2_run-3"):
        h.Task(id=good, prompt="x")
```

**Verify**:
- `python3 test_skill_ab_harness.py` → ends with `54 passed` and includes
  `ok  test_task_id_path_traversal_rejected` and
  `ok  test_task_id_normal_accepted`.
- `uvx ruff check skill_ab_harness.py 2>&1 | grep -c E702` → `4` (no new errors).

### Step 2: Guard the strip-chart mean marker against a null mean

In `stripChart`, replace the unconditional mean-marker block at
`skill_ab_harness.py:2755-2761`. Replace exactly this:

```javascript
      var my = y(mv);
      var meanMark = "<line x1='" + (cx - band * 0.32) + "' y1='" + my.toFixed(1)
        + "' x2='" + (cx + band * 0.32) + "' y2='" + my.toFixed(1) + "' stroke='" + c
        + "' stroke-width='2.4'/><text class='gtxt-strong' x='"
        + (cx + band * 0.32 + 4) + "' y='" + (my + 4).toFixed(1) + "' fill='" + c
        + "'>" + fmtVal(m, mv) + "</text>";
```

with this (ES5 `var`/`if`, every line < 100 cols):

```javascript
      // No ITT-valid runs -> mean is null; skip the marker instead of drawing a
      // bogus one at 0 with a "—" label (barChart drops such arms entirely).
      var meanMark = "";
      if (mv != null) {
        var my = y(mv);
        meanMark = "<line x1='" + (cx - band * 0.32) + "' y1='" + my.toFixed(1)
          + "' x2='" + (cx + band * 0.32) + "' y2='" + my.toFixed(1) + "' stroke='" + c
          + "' stroke-width='2.4'/><text class='gtxt-strong' x='"
          + (cx + band * 0.32 + 4) + "' y='" + (my + 4).toFixed(1) + "' fill='" + c
          + "'>" + fmtVal(m, mv) + "</text>";
      }
```

This keeps the per-run dots for invalid runs (the strip chart's purpose is to
show every run) while suppressing only the meaningless mean line/label. Do not
change the `arms` filter at line 2719-2721.

**Verify**:
- `uvx ruff check skill_ab_harness.py 2>&1 | grep -c E702` → `4` (ruff lints the
  Python file; the edit must not change line count in a way that adds errors — it
  does not).
- `python3 test_skill_ab_harness.py` → still passes (no JS unit tests exist; this
  confirms you did not break the Python that builds the string).
- `grep -n 'if (mv != null) {' skill_ab_harness.py` → one match in `stripChart`.

### Step 3: Use `python3` in docs

Replace bare `python ` invocations with `python3 ` in the four locations under
"Step 3 facts". Concretely:
- `README.md:13`: `python skill_ab_harness.py demo` → `python3 skill_ab_harness.py demo`
  (preserve the trailing `# or: skills-test demo  (after pip/uvx install)` comment).
- `README.md:110`: `python test_skill_ab_harness.py` → `python3 test_skill_ab_harness.py`.
- `CLAUDE.md:99`: `python test_skill_ab_harness.py` → `python3 test_skill_ab_harness.py`.
- `test_skill_ab_harness.py:6`: `python -m pytest -q` or `python
  test_skill_ab_harness.py` → `python3 -m pytest -q` or `python3
  test_skill_ab_harness.py` (this is a docstring; do not touch any executable
  line). Note: this repo's runner is NOT pytest — leave the `-m pytest` mention
  as-is (just `python` → `python3`); it is illustrative.

Docs/comment only — no code behavior changes.

**Verify**:
- `grep -rn 'python ' README.md CLAUDE.md test_skill_ab_harness.py` → no line
  shows a bare `python ` used as a command (matches like `python3 ` are fine;
  prose mentioning "Python" the language is fine). Eyeball the four cited lines.
- `python3 test_skill_ab_harness.py` → still passes (docstring edit is inert).

### Step 4: Delete the dead `_pooled_mean_diff` helper

First confirm the premise:
`grep -rn "_pooled_mean_diff" /Users/copyjosh/Code/skills-test` must return
exactly two lines — the definition at `skill_ab_harness.py:1096` and the single
test caller at `test_skill_ab_harness.py:96`. **If it returns any third match
(a production caller), STOP** (see STOP conditions).

Then:
1. Delete the function body `skill_ab_harness.py:1096-1100` (the 5 lines shown in
   "Step 4 facts"), including the blank line that separates it from the next
   `def cluster_bootstrap_ci(`. Leave exactly one blank line between the
   preceding function and `cluster_bootstrap_ci`.
2. In `test_skill_ab_harness.py`, in `test_permutation_p_small_for_strong_effect`,
   replace:
   ```python
       point = h._pooled_mean_diff(on, off, ["a", "b"])
       assert point == 1.0
   ```
   with:
   ```python
       point = 1.0   # pooled mean diff of all-1 ON vs all-0 OFF
   ```
   (The `assert point == 1.0` becomes redundant once `point` is the literal, so
   drop it. The rest of the test — the `cluster_permutation_p` assertions — is
   unchanged and still does the real work.)

**Verify**:
- `grep -rn "_pooled_mean_diff" /Users/copyjosh/Code/skills-test` → no matches.
- `python3 test_skill_ab_harness.py` → passes; `ok
  test_permutation_p_small_for_strong_effect` still listed.
- `uvx ruff check skill_ab_harness.py 2>&1 | grep -c E702` → `4`.

## Test plan

- **Step 1 (new tests)** in `test_skill_ab_harness.py`:
  - `test_task_id_path_traversal_rejected` — the regression: a `../evil` id (the
    actual exploit shape) raises `ValueError`.
  - `test_task_id_normal_accepted` — happy path: every id the harness itself uses
    still constructs.
  - Pattern source: `test_skill_a_equals_skill_b_rejected`
    (`test_skill_ab_harness.py:687`).
- **Step 4** edits an existing test (`test_permutation_p_small_for_strong_effect`)
  to drop the deleted helper while preserving its `cluster_permutation_p`
  assertions.
- **Steps 2 & 3** have no automated test (the JS string is not unit-tested by the
  stdlib runner; the docs are prose). Their verification is the grep + the fact
  that `python3 test_skill_ab_harness.py` still passes.
- Full-suite verification: `python3 test_skill_ab_harness.py` → `54 passed`
  (53 existing + 1 net new: Step 1 adds two tests, Step 4 adds none and removes
  none).

## Done criteria

Machine-checkable. ALL must hold for the steps you performed:

- [ ] `python3 test_skill_ab_harness.py` exits 0 and prints `54 passed` (if Step 1
      done) or `53 passed` (if Step 1 skipped).
- [ ] If Step 1 done: `ok  test_task_id_path_traversal_rejected` and
      `ok  test_task_id_normal_accepted` appear in the output.
- [ ] If Step 4 done: `grep -rn "_pooled_mean_diff"
      /Users/copyjosh/Code/skills-test` returns no matches.
- [ ] If Step 2 done: `grep -n 'if (mv != null) {' skill_ab_harness.py` returns
      one match.
- [ ] If Step 3 done: the four cited doc lines use `python3`, not bare `python`,
      as the command.
- [ ] `uvx ruff check skill_ab_harness.py 2>&1 | grep -c E702` is `4` and no
      non-`E702` codes appear (you introduced zero new lint errors).
- [ ] No file outside the in-scope list was modified.
- [ ] Your row in `plans/README.md` is updated by the reviewer (not you).

## STOP conditions

Stop and report back (do not improvise) if:

- The live code at any cited `file:line` does not match the "Current state"
  excerpt (the file drifted since this plan was written).
- `grep -rn "_pooled_mean_diff"` (Step 4) shows ANY caller other than the single
  test line at `test_skill_ab_harness.py:96` — the dead-code premise is then
  false; do not delete.
- Adding the `Task.__post_init__` guard (Step 1) makes any EXISTING test fail —
  that means a current Task id violates the charset; report which one rather than
  loosening the regex.
- `uvx ruff check` reports more than 4 errors after your edit, or any code other
  than `E702` — your edit introduced a new lint error; fix it or report.
- A step's verification fails twice after a reasonable fix attempt.

## Maintenance notes

For whoever owns this code next:

- **Step 1**: the `Task.__post_init__` guard is the single chokepoint for both
  local TOML and `--from-github` remote configs (both flow through `Task(**t)` in
  `load_config`). If a new Task construction path is added that must accept a
  different id charset (e.g. slashes for namespacing), change the regex here, not
  at each interpolation site — the path/branch/artifact interpolations
  (lines 413-414, 823, 843, 850) all assume the id is already filesystem- and
  ref-safe.
- **Step 2**: `stripChart` and `barChart` now agree on which arms have a drawable
  mean. If a future change makes `armMeans` populate for invalid runs, revisit
  both filters together.
- **Reviewer should scrutinize**: that the Step 1 regex rejects `/` and `..` but
  still accepts every id the demo and tests use (run the suite), and that the
  Step 2 JS stays ES5 and < 100 cols.
- **Deferred**: the 4 pre-existing `E702` lint errors are intentionally left
  alone here; if a separate cleanup plan fixes them, this plan's "Found 4 errors"
  baseline becomes "Found 0 errors" — adjust the Done criteria reference then.
