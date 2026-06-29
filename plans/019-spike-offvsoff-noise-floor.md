# Plan 019: Spike — define an OFF-vs-OFF (control-vs-control) noise-floor run mode

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, your status row will be added to
> `plans/README.md` by the reviewer — do NOT edit `plans/README.md` yourself.
>
> **This is a DESIGN / SPIKE plan, not a build-everything plan.** Your job is to
> (a) confirm the design analysis below against the live code, (b) add a tiny
> prototype/validation (two deterministic tests) that proves the existing
> estimator stack can carry two control arms and yields a *calibrated* false-
> positive rate, and (c) record the recommended design + open questions as a
> new subsection in `METHODOLOGY.md`. You are NOT shipping the run mode itself.
> Do NOT add an `Arm` enum member, a CLI flag, or a new command in this plan.
>
> **Drift check (run first)**: the repo is NOT git-initialized, so there is no
> SHA to diff against. Instead, open `skills_test.py` and compare the
> "Current state" excerpts below to the live code before editing. On any
> mismatch (line numbers may move; the *code* should match), treat it as a STOP
> condition and report what changed.

## Status

- **Priority**: P3
- **Effort**: M (spike)
- **Risk**: LOW
- **Depends on**: none
- **Category**: direction
- **Planned at**: not git-initialized, 2026-06-25

## Why this matters

Today a user who ships a skill and gets a grey/null badge cannot tell two very
different things apart: "my skill genuinely did nothing" versus "this task +
model is so noisy that *no* effect of any size would have been detectable." The
honest, credible answer to "is my green badge luck?" is an empirical
**false-positive rate**: run two *identical* no-skill control arms against each
other and measure how often the 95% CI spuriously excludes 0, plus the measured
noise SD. The harness already gestures at this — `within_arm_noise()` is a
stopgap proxy and `minimum_detectable_effect()` simulates it with *synthetic*
Gaussian noise — but there is no run mode that measures the floor from *real*
control runs. `plans/README.md` lists this as deferred future work and
`METHODOLOGY.md` points at the within-arm-spread proxy instead. This spike
nails down the config/CLI surface, the exact reporting, the open questions, and
proves with a runnable prototype that the existing arm/estimator machinery can
carry two controls — so a later implementer can build it without re-deriving
any of this.

## Current state

Single module `skills_test.py` (~3715 lines). Tests in
`test_skills_test.py` (custom stdlib runner — see "Commands"). The relevant
machinery:

- **The `Arm` enum has exactly three members, and only ONE control**
  (`skills_test.py:91-94`):

  ```python
  class Arm(str, Enum):
      SKILL_ON = "skill_on"     # the (primary) skill: cfg.skill_src / cfg.skill_name
      SKILL_OFF = "skill_off"   # the no-skill control
      SKILL_B = "skill_b"       # a SECOND skill (head-to-head): cfg.skill_b_src / skill_b_name
  ```

  There is no second *control* identity. Two controls would both have to be
  `SKILL_OFF`, which collides everywhere an arm is keyed by identity or label
  (see below). This is the core constraint the design must solve.

- **`__post_init__` forbids expressing "two controls" via the `skill_b` fields**
  (`skills_test.py:170-178`):

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

  `skill_b` *requires a name*, so you cannot smuggle a no-skill second arm
  through it — the half-set guard rejects `skill_b_src` without a name, and a
  noise-floor mode is conceptually a *control*, not "skill B". A new arm
  identity is required; reusing `skill_b` is a dead end (note this in the design
  doc).

- **Arm → skill / label / pairs all key off `Arm` identity or `arm_label`**
  (`skills_test.py:189-235`):

  ```python
  def experiment_arms(cfg: ExperimentConfig) -> list[Arm]:
      if _is_head_to_head(cfg):
          return [Arm.SKILL_ON, Arm.SKILL_B, Arm.SKILL_OFF]
      return [Arm.SKILL_ON, Arm.SKILL_OFF]

  def arm_skill(cfg: ExperimentConfig, arm: Arm) -> tuple[Path | None, str | None]:
      """(skill_src, skill_name) this arm installs; (None, None) for the control."""
      if arm is Arm.SKILL_ON:
          return cfg.skill_src, cfg.skill_name
      if arm is Arm.SKILL_B:
          return cfg.skill_b_src, cfg.skill_b_name
      return None, None

  def arm_label(cfg: ExperimentConfig, arm: Arm) -> str:
      """Human label for an arm: the skill name, or 'control' for the no-skill arm."""
      _src, name = arm_skill(cfg, arm)
      return name or "control"

  def experiment_pairs(cfg: ExperimentConfig) -> list[tuple[Arm, Arm]]:
      if _is_head_to_head(cfg):
          return [(Arm.SKILL_ON, Arm.SKILL_OFF), (Arm.SKILL_B, Arm.SKILL_OFF),
                  (Arm.SKILL_ON, Arm.SKILL_B)]
      return [(Arm.SKILL_ON, Arm.SKILL_OFF)]

  def primary_pair(cfg: ExperimentConfig) -> tuple[Arm, Arm]:
      return (Arm.SKILL_ON, Arm.SKILL_B) if _is_head_to_head(cfg) else (Arm.SKILL_ON, Arm.SKILL_OFF)

  def pair_key(cfg: ExperimentConfig, a: Arm, b: Arm) -> str:
      return f"{arm_label(cfg, a)}_vs_{arm_label(cfg, b)}"
  ```

  Two control arms would both yield `arm_label(...) == "control"`, so
  `pair_key` collapses to `"control_vs_control"` AND the `arm_stats` dict (keyed
  by `arm_label`, below) would merge the two arms into a single `"control"` key.
  This is the **label-collision** the design must resolve (distinct labels such
  as `control_a` / `control_b`).

- **The run loop and the worktree label key off `arm.value`**
  (`skills_test.py:1052-1058` and `:850`):

  ```python
  jobs = [
      run_and_persist(task, arm, i)
      for task in tasks
      for arm in experiment_arms(cfg)
      for i in range(cfg.k)
      if (task.id, arm.value, i) not in done
  ]
  ```

  ```python
  label = f"{task.id}-{arm.value}-{idx}"   # -> Worktree dir + branch ab/<label>
  ```

  Distinct `Arm` members get distinct worktree dirs/branches; two controls
  sharing `SKILL_OFF` would collide here too. The whole pipeline runs
  `for arm in experiment_arms(cfg)`, so adding a second control arm flows
  through run, estimate, summary, badge, and report *for free* — IF it has a
  distinct `Arm` identity and a distinct label.

- **Summary builds comparisons per pair and arm_stats per label**
  (`skills_test.py:1685-1709`):

  ```python
  comparisons = {}
  for a, b in experiment_pairs(cfg):
      comparisons[pair_key(cfg, a, b)] = {
          "a": arm_label(cfg, a), "b": arm_label(cfg, b),
          **_pair_estimates(results, cfg, metrics, rng, a, b)}

  arm_stats = {}
  for arm in experiment_arms(cfg):
      fired, clean = activation_rate(results, arm)
      arm_stats[arm_label(cfg, arm)] = { ... }

  pa, pb = primary_pair(cfg)
  pkey = pair_key(cfg, pa, pb)
  ```

- **The core estimator** every layer rests on
  (`skills_test.py:1103-1110`):

  ```python
  def cluster_bootstrap_ci(on: dict[str, list[float]], off: dict[str, list[float]],
                           shared: list[str], iters: int, alpha: float,
                           rng: random.Random) -> tuple[float, float, bool]:
      """Resample TASKS with replacement, then runs within each sampled task (per
      arm). ... Falls back to flat run-level resampling when <2 shared tasks; that
      path is anticonservative, flagged via the returned bool."""
  ```

  Returns `(ci_low, ci_high, clustered)`. With **<2 shared tasks** it drops to
  flat run-level resampling, which is anticonservative — relevant to the
  "tasks>=2?" open question.

- **The synthetic noise simulator already does most of the math**
  (`skills_test.py:3184-3206`) — it draws ON and OFF from the same/shifted
  center and counts how often the CI excludes 0. The noise-floor mode is the
  *empirical* analogue (real control runs instead of `rng.gauss`):

  ```python
  def minimum_detectable_effect(cfg, n_tasks, baseline, noise_sd, power=0.8,
                                seed=0, deltas=None, sims=200):
      ...
      def draw(center: float) -> dict:
          return {t: [center + rng.gauss(0, noise_sd) for _ in range(cfg.k)] for t in tasks}
      for d in deltas:
          hits = 0
          for _ in range(sims):
              on, off = draw(baseline + d), draw(baseline)
              lo, hi, _ = cluster_bootstrap_ci(on, off, tasks, 400, cfg.bootstrap_alpha, rng)
              if not (lo <= 0 <= hi):
                  hits += 1
          if hits / sims >= power:
              return d
      return None
  ```

- **The current stopgap proxy** (`skills_test.py:3209-3214`):

  ```python
  def within_arm_noise(results: list[RunResult], metric: str, arm: Arm) -> float:
      """Std dev of a metric within one arm's valid runs — a quick noise indicator
      (used by the methodology / power guidance)."""
      vals = [r.scores[metric] for r in results
              if r.arm is arm and r.itt_valid and metric in r.scores]
      return statistics.pstdev(vals) if len(vals) > 1 else 0.0
  ```

- **`METHODOLOGY.md:51-55`** currently documents only the synthetic-MDE proxy
  and names `within_arm_noise` as the prior for `N`. The deferred-work note is
  at **`plans/README.md:68-70`** ("A dedicated OFF-vs-OFF noise-floor *run mode*
  remains future work").

### Repo conventions that apply here

- **STDLIB-ONLY is a hard rule.** Never add a dependency (no numpy / pandas /
  pytest). Python >=3.11.
- Comments explain **why**, not what.
- Tests are plain `test_*` functions collected and called with **no args** by a
  custom runner (`test_skills_test.py` bottom: `_run_all()` iterates
  `globals()` for `test_`-prefixed callables and calls `fn()`). There is no
  pytest, no fixtures. Use the existing helpers `_cfg(**kw)` and
  `_rr(arm, activated, **kw)` (see `test_skills_test.py:18-45`); seed your
  own `random.Random(...)` for determinism. Keep every line <100 cols.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Tests   | `python3 test_skills_test.py` | runs the stdlib runner; prints `N passed`, exit 0 |
| Lint    | `uvx ruff check skills_test.py` | `All checks passed!`, exit 0 |
| Lint tests | `uvx ruff check test_skills_test.py` | `All checks passed!`, exit 0 |

(There is no `git`, no `npm`, no network involved. Tests need neither `claude`
nor `git`.)

## Scope

**In scope** (the only files you should modify):
- `test_skills_test.py` — add the two prototype/validation tests (Step 2, 3).
- `METHODOLOGY.md` — add one "Noise floor (OFF-vs-OFF)" subsection recording the
  recommended design + open questions (Step 4).

**Out of scope** (do NOT touch, even though they look related):
- `skills_test.py` — this is a SPIKE. Do NOT add an `Arm` member, a
  `noise_floor`/`--null` config field/flag, a `null` subparser, or change
  `experiment_arms`/`arm_label`/`experiment_pairs`/`primary_pair`. The point of
  the spike is to *design and de-risk*, not implement. (Implementation is a
  follow-up plan; see Maintenance notes.)
- `plans/README.md` — the reviewer maintains the index; do not edit it.
- `plans/001`–`plans/017` — DONE; never touch.
- The badge gate logic — the recommendation (Step 4) is explicitly NOT to gate
  the badge on the noise floor; do not wire anything into it.

## Git workflow

The repo is **not** git-initialized — there is no branch, no commit, no PR.
Just edit the in-scope files in place and run the verification commands. Do not
run `git init` or attempt to commit.

## Steps

### Step 1: Confirm the design analysis against the live code (read-only)

Open `skills_test.py` and confirm each excerpt in "Current state" matches
the live code (line numbers may have shifted; the *code* must match). In
particular confirm: (a) `Arm` has exactly three members with one control
(`:91-94`); (b) `arm_label` returns `name or "control"` (`:206-209`); (c)
`pair_key` is `f"{arm_label(a)}_vs_{arm_label(b)}"` (`:234-235`); (d) the run
loop iterates `experiment_arms(cfg)` (`:1052-1058`); (e) the worktree label is
`f"{task.id}-{arm.value}-{idx}"` (`:850`).

This confirms the central design conclusion you will record in Step 4:

> **Recommended design = a new control-only `Arm` identity, NOT a reuse of
> `skill_b`.** The cleanest surface is a `noise_floor: bool` field on
> `ExperimentConfig` plus a second control arm (e.g. a new `Arm.SKILL_OFF_B`)
> whose `arm_skill` returns `(None, None)` and whose `arm_label` returns a
> *distinct* label (`control_a` / `control_b`). Then `experiment_arms`,
> `experiment_pairs`, and `primary_pair` all special-case the noise-floor config
> to `[SKILL_OFF, SKILL_OFF_B]` / `[(SKILL_OFF, SKILL_OFF_B)]`, and the entire
> run → estimate → summary → badge → report pipeline carries it unchanged
> because every layer already iterates `experiment_arms`/`experiment_pairs`. The
> contamination rule is already correct for a control (any skill firing on a
> control = contaminated, `arm_skill_name is None`), so it applies symmetrically
> to both noise-floor arms with no change.

**Verify**: no command — this is a read. If any excerpt does not match the live
code, STOP and report (drift). Otherwise proceed.

### Step 2: Prototype A — prove the estimator yields a *calibrated* false-positive rate for two equal-mean controls

Add a deterministic test to `test_skills_test.py` named
`test_noise_floor_false_positive_rate_is_calibrated`. It must feed the REAL core
estimator `cluster_bootstrap_ci` two control draws from the **same** center
(delta truly 0) over many trials and confirm the fraction of trials whose 95% CI
excludes 0 is near `alpha` (=0.05). This is the empirical premise of the whole
mode: two controls produce a *calibrated* spurious-significance rate, so a
measured rate well above the floor would be a real signal.

Use `random.Random(seed)` for determinism. Mirror the simulation shape in
`minimum_detectable_effect` (`skills_test.py:3184-3206`): build
`on`/`off` as `{task: [center + gauss(0, sd) for _ in range(k)]}` with **equal**
centers, call `cluster_bootstrap_ci(on, off, tasks, iters, alpha, rng)`, and
count `not (lo <= 0 <= hi)`. Use >=2 tasks so the clustered (conservative) path
is exercised, a few hundred trials, and a loose tolerance band (the bootstrap CI
is approximate — assert the rate is below roughly 0.15, i.e. it does NOT
massively over-reject; do not assert an exact 0.05). Target shape:

```python
def test_noise_floor_false_positive_rate_is_calibrated():
    # OFF-vs-OFF premise: two controls drawn from the SAME center produce a
    # CALIBRATED spurious-"significant" rate (~alpha), so a rate far above the
    # floor in a real run would be a genuine signal, not noise.
    rng = random.Random(7)
    cfg = _cfg(k=6, bootstrap_alpha=0.05)
    tasks = ["t0", "t1", "t2"]
    sd, center, trials, excl = 0.2, 0.5, 300, 0
    for _ in range(trials):
        def draw():
            return {t: [center + rng.gauss(0, sd) for _ in range(cfg.k)] for t in tasks}
        lo, hi, clustered = h.cluster_bootstrap_ci(
            draw(), draw(), tasks, 400, cfg.bootstrap_alpha, rng)
        assert clustered  # >=2 tasks -> conservative clustered path, not flat
        if not (lo <= 0 <= hi):
            excl += 1
    rate = excl / trials
    assert rate < 0.15, f"false-positive rate {rate} too high for equal-mean controls"
```

**Verify**: `python3 test_skills_test.py` → prints `ok
test_noise_floor_false_positive_rate_is_calibrated` and ends `N passed`, exit 0.

### Step 3: Prototype B — pin the label-collision constraint into an executable check

Add a second deterministic test named
`test_two_controls_collapse_under_current_arm_labels`. It must demonstrate, as
an executable fact, *why* a new arm identity is required: under today's model,
the only control is `Arm.SKILL_OFF` and `arm_label` for any control is
`"control"`, so two controls cannot be told apart by label or `pair_key`. This
locks the design constraint so a future refactor that breaks it fails a test.
Target shape:

```python
def test_two_controls_collapse_under_current_arm_labels():
    # SPIKE GUARD (plan 019): today there is exactly ONE control identity, and
    # every control labels as "control". A noise-floor mode therefore CANNOT
    # reuse the existing control arm for both sides -- it needs a distinct second
    # control identity + distinct labels (control_a/control_b). This pins that.
    cfg = _cfg()
    assert h.arm_label(cfg, Arm.SKILL_OFF) == "control"
    # arm_skill returns (None, None) for the control -> any second control would
    # also label "control", collapsing pair_key to control_vs_control.
    assert h.arm_skill(cfg, Arm.SKILL_OFF) == (None, None)
    assert h.pair_key(cfg, Arm.SKILL_OFF, Arm.SKILL_OFF) == "control_vs_control"
    # And skill_b cannot express a no-skill second arm: it requires a name.
    import pytest_is_not_used  # noqa: F401  -- placeholder; replace per note below
```

Remove the placeholder line; instead assert the `__post_init__` guard rejects a
nameless `skill_b` using a try/except (no pytest available — this runner is
stdlib). Use this pattern for the last assertion:

```python
    raised = False
    try:
        _cfg(skill_b_src=Path("/skills/x"))  # name omitted -> half-set, rejected
    except ValueError:
        raised = True
    assert raised, "skill_b without a name should be rejected (cannot be a 2nd control)"
```

**Verify**: `python3 test_skills_test.py` → prints `ok
test_two_controls_collapse_under_current_arm_labels` and ends `N passed`,
exit 0.

### Step 4: Record the design decision + open questions in `METHODOLOGY.md`

Add ONE new subsection to `METHODOLOGY.md` (place it directly after the
"Is my effect real, or am I underpowered?" section, i.e. after line 55). Do NOT
create a separate doc. The subsection must answer the spike's design questions
concisely. Required content (paraphrase into prose/bullets; keep lines readable):

1. **Config / CLI surface** — recommended: a `noise_floor: bool` on
   `ExperimentConfig` (and a `skills-test run --null` flag that sets it), which
   selects a two-control experiment: `experiment_arms` →
   `[Arm.SKILL_OFF, <new SKILL_OFF_B>]`, both `arm_skill` → `(None, None)`,
   labelled `control_a` / `control_b` to avoid the `control_vs_control`
   collision documented in `test_two_controls_collapse_under_current_arm_labels`.
   It reuses `experiment_pairs` / the existing estimator/badge/report stack
   unchanged (every layer iterates `experiment_arms`/`experiment_pairs`). It
   does NOT and cannot reuse the `skill_b` fields — those require a name and
   `__post_init__` rejects a half-set pair (`skills_test.py:170-178`).
2. **What it reports** — the per-task delta distribution between the two
   controls, the measured noise SD (pooled `within_arm_noise` over both control
   arms), and the **empirical false-positive rate**: how often the 95% CI
   spuriously excludes 0. State the subtlety proven in Step 2: a *single*
   OFF-vs-OFF run yields ONE delta + ONE CI (a single Bernoulli draw), so the
   *rate* must come from many independent control comparisons (many tasks)
   and/or a label-shuffle/permutation null over the pooled control runs — the
   empirical analogue of `minimum_detectable_effect`'s synthetic loop
   (`skills_test.py:3184-3206`).
3. **Open questions** (record, do not resolve): (a) minimum `k` for a stable CI;
   (b) `tasks>=2` is effectively required — with one task `cluster_bootstrap_ci`
   drops to the anticonservative flat path (`skills_test.py:1112-1116`), so
   a single-task floor understates noise; (c) how to present the floor next to a
   real run (recommend a one-line "noise floor: spurious-significant X% of the
   time; your delta = Y" annotation in the report); (d) whether to **gate** the
   badge on it — recommend **NO** (keep the badge self-policing; the floor is an
   interpretability aid, optionally a soft WARN when the real delta is below the
   measured floor, never a hard gate).
4. A pointer that this is a SPIKE conclusion (plan 019); implementation is
   deferred to a follow-up plan.

**Verify**: `grep -n "Noise floor" METHODOLOGY.md` returns at least one line
(the new subsection heading exists).

## Test plan

- New tests in `test_skills_test.py`:
  - `test_noise_floor_false_positive_rate_is_calibrated` (Step 2) — happy-path
    premise: two equal-mean controls through the REAL `cluster_bootstrap_ci`
    yield a calibrated (not inflated) spurious-exclusion rate; also asserts the
    clustered (>=2 task) path is taken.
  - `test_two_controls_collapse_under_current_arm_labels` (Step 3) — guard /
    constraint: confirms there is one control identity, both controls label
    `control`, `pair_key` collapses to `control_vs_control`, and `skill_b`
    cannot express a nameless second control (the `__post_init__` guard fires).
- Structural pattern to copy: the existing `test_mde_monotone`
  (`test_skills_test.py:457-461`) for calling the estimator/simulator with
  a seeded `cfg`, and `test_head_to_head_requires_both_skill_b_fields`
  (`:561`) for the try/except-on-`ValueError` shape (stdlib, no pytest).
- These tests are deterministic (seeded `random.Random`), stdlib-only, and need
  no `claude`/`git`/network — consistent with the suite's contract.
- Verification: `python3 test_skills_test.py` → all pass including the 2
  new tests; `N passed` increases by 2 from the prior count.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `python3 test_skills_test.py` exits 0 and prints `N passed`, where N
      is the previous count + 2 (the two new tests both appear as `ok ...`).
- [ ] `uvx ruff check skills_test.py` prints `All checks passed!`, exit 0.
- [ ] `uvx ruff check test_skills_test.py` prints `All checks passed!`,
      exit 0.
- [ ] `grep -c "def test_noise_floor_false_positive_rate_is_calibrated\|def
      test_two_controls_collapse_under_current_arm_labels"
      test_skills_test.py` returns `2`.
- [ ] `grep -n "Noise floor" METHODOLOGY.md` returns >=1 line, and that
      subsection answers items 1–4 in Step 4.
- [ ] `skills_test.py` is UNCHANGED (no `Arm` member, config field, flag,
      or function edits) — this is a spike. Verify by reading: no diff to that
      file.
- [ ] `plans/README.md` and `plans/001`–`plans/017` are unchanged.

## STOP conditions

Stop and report back (do not improvise) if:

- Any "Current state" excerpt does not match the live `skills_test.py`
  (drift — the design may no longer hold).
- `Arm` has gained a control-only member, or `noise_floor` / `--null` already
  exists — the mode may have been partly implemented since this plan was
  written; report and do not duplicate it.
- `test_noise_floor_false_positive_rate_is_calibrated` fails because the
  measured rate is well above the tolerance band (e.g. >0.15) — this would mean
  the bootstrap CI is *not* calibrated for two equal-mean controls, which
  undermines the whole premise. Report the observed rate; do not "fix" it by
  loosening the band past 0.2 without flagging it.
- A step's verification fails twice after a reasonable fix attempt.
- Completing a step appears to require editing `skills_test.py` (it should
  not — if it does, the spike boundary is wrong; report it).

## Maintenance notes

For the owner of the follow-up *implementation* plan (deferred out of this
spike on purpose):

- The implementation should add `Arm.SKILL_OFF_B` (control-only), a
  `noise_floor: bool` config field + `run --null` flag, and special-case
  `experiment_arms` / `arm_label` (distinct `control_a`/`control_b`) /
  `experiment_pairs` / `primary_pair` for the noise-floor config. Because the
  run/estimate/summary/badge/report layers all iterate
  `experiment_arms`/`experiment_pairs` (`skills_test.py:1052-1058`,
  `:1685-1709`, `:2968`, `:3087`), they should carry it with no per-call-site
  edits — verify that.
- The empirical false-positive RATE needs many independent control comparisons;
  a single OFF-vs-OFF run is one Bernoulli draw. The implementer should either
  require >=2 tasks (avoid the anticonservative flat bootstrap path at
  `skills_test.py:1112-1116`) or add a label-shuffle null over pooled
  control runs (the empirical analogue of `minimum_detectable_effect`).
- Keep the badge self-policing: do NOT gate it on the floor. A soft WARN when
  the real delta is below the measured floor is acceptable; a hard gate is not.
- `test_two_controls_collapse_under_current_arm_labels` (Step 3) will need to be
  updated when the second control identity lands (the `control_vs_control`
  collapse it pins is intentionally fixed *by* the implementation). Leave a note
  in that test referencing the implementation plan number when it exists.
- A reviewer should scrutinize: that the contamination rule stays
  arm-symmetric for both controls (any skill firing = contaminated), and that
  the noise-floor summary is clearly labelled so it is never mistaken for a real
  skills-test result.
