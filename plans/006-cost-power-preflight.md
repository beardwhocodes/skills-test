# Plan 006: `--plan` — cost/time dry-run, budget ceiling, and minimum-detectable-effect

> **Executor instructions**: Follow step by step; verify each step; honor STOP
> conditions; update this plan's row in `plans/README.md` when done.
>
> **Drift check**: repo not git-tracked. Confirm `run_experiment`,
> `run_preflight`/`Preflight`, `cluster_bootstrap_ci`, and `ExperimentConfig`
> match the excerpts before editing. Mismatch → STOP.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: MED (touches the run loop for the budget ceiling)
- **Depends on**: 001 (CLI), 002 (manifest, optional)
- **Category**: direction / trust + footgun-removal
- **Planned at**: 2026-06-25 (repo not git-initialized)

## Why this matters

`k` defaults to 5, so `tasks × 2 arms × 5` real `claude -p` runs cost real money
with **zero forecast today** and no ceiling — a footgun that stops people running
it ("might silently cost $40") and stops them sharing it ("don't run this rando's
script, who knows what it'll spend"). This plan adds: (a) a `--plan`/`--dry-run`
that prints a projected `$`/wall-clock BEFORE spending, (b) a hard
`cost_ceiling_usd` that aborts mid-run and keeps what's done, and (c) a
**minimum-detectable-effect** readout that reuses the existing cluster estimators
to answer "is my k big enough to detect the effect I care about?" — the rigor that
lets an author defend a NULL result instead of being told it's underpowered.

## Current state

- `class ExperimentConfig` (`skills_test.py:105`) — no `cost_ceiling_usd`.
- `run_experiment(cfg, tasks, scorers=None)` (`:725`) fires all
  `tasks × 2 × k` jobs via `asyncio.gather(*jobs, return_exceptions=True)` (~line 755),
  each through `run_and_persist` (~line 737). No budget guard.
- `RunResult.cost_usd` (`:155`ish) is captured per run, summed nowhere.
- `run_preflight(cfg, tasks, scorers)` (`:515`) already runs scorers on the clean
  base, capturing per-(task,scorer) values across `preflight_repeats` — this
  variance is the natural power prior (currently used only for flaky/red quarantine).
- `cluster_bootstrap_ci(on, off, shared, iters, alpha, rng)` (`:799`) and
  `cluster_permutation_p(...)` (`:834`) — reusable estimators over arbitrary draws.
- `Preflight` dataclass (`:506`): `quarantined`, `notes`.
- Convention: stdlib only (`statistics`, `random`).

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Compile | `python3 -m py_compile skills_test.py test_skills_test.py` | exit 0 |
| Tests | `python3 test_skills_test.py` | `N passed` |
| Line length | plan-001 Step-5 snippet | `OK` |

## Scope

**In scope:** `skills_test.py` (add `cost_ceiling_usd` field; `estimate_cost`;
`minimum_detectable_effect`; a budget guard in the run loop; a `plan` subcommand);
`test_skills_test.py` (cost-math + MDE tests).

**Out of scope:** the scorers, the worktree/agent mechanics, the report format.

## Steps

### Step 1: Add the ceiling field

In `ExperimentConfig`, near the other knobs:
```python
    cost_ceiling_usd: float | None = None   # abort remaining runs once total cost crosses this
    cost_per_run_usd: float | None = None   # override the dry-run cost prior (else measured)
```
**Verify**: `python3 -c "import skills_test as h; from pathlib import Path; \
print(h.ExperimentConfig(repo_path=Path('/r'),base_ref='m',skill_src=Path('/s'),skill_name='k').cost_ceiling_usd)"` → `None`.

### Step 2: Cost/time projection

```python
def estimate_cost(cfg: ExperimentConfig, tasks: list[Task],
                  per_run_usd: float | None = None,
                  per_run_seconds: float | None = None) -> dict:
    """Project total spend/time. per_run_* come from a measured calibration run if
    not given (caller may pass cfg.cost_per_run_usd). Judge calls are counted when
    judge_enabled: 2 orderings × min(k, judge_max_pairs_per_task) pairs × tasks."""
    n_runs = len(tasks) * 2 * cfg.k
    n_judge = (2 * min(cfg.k, cfg.judge_max_pairs_per_task) * len(tasks)
               if cfg.judge_enabled else 0)
    cpr = per_run_usd if per_run_usd is not None else cfg.cost_per_run_usd
    total_cost = None if cpr is None else cpr * (n_runs + n_judge)
    wall = None
    if per_run_seconds is not None:
        import math
        waves = math.ceil((n_runs + n_judge) / max(1, cfg.max_concurrency))
        wall = per_run_seconds * waves
    return {"n_runs": n_runs, "n_judge_calls": n_judge,
            "per_run_usd": cpr, "projected_usd": total_cost,
            "projected_wall_seconds": wall, "ceiling_usd": cfg.cost_ceiling_usd}
```

**Verify** (Step 6 test): 2 tasks, k=5, judge off → `n_runs == 20`;
`per_run_usd=0.31` → `projected_usd == 6.2`.

### Step 3: Minimum-detectable-effect via the existing estimators

Use the preflight variance (or a supplied baseline) to simulate: for a candidate
true effect δ, draw synthetic per-task on/off run values around (baseline) and
(baseline+δ) with the observed noise, run `cluster_bootstrap_ci`, and find the
smallest δ whose 95% CI excludes 0 in ≥80% of simulations at the current k/tasks.
```python
def minimum_detectable_effect(cfg, n_tasks: int, baseline: float, noise_sd: float,
                              power: float = 0.8, seed: int = 0,
                              deltas=None, sims: int = 200) -> float | None:
    """Smallest effect detectable at `power` for the current k and n_tasks, given a
    per-run noise SD (e.g. from preflight). Reuses cluster_bootstrap_ci — no numpy."""
    import random, statistics
    rng = random.Random(seed)
    deltas = deltas or [i / 100 for i in range(1, 51)]  # 0.01 .. 0.50
    tasks = [f"t{i}" for i in range(n_tasks)]
    def draw(center):
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
(Choosing `baseline`/`noise_sd`: derive from a pilot `results.jsonl` if present,
else accept `--baseline`/`--noise` args. Keep `sims`/iters modest — this is a
planning aid, not the final inference.)

**Verify** (Step 6 test): with a tiny `noise_sd` MDE is small; with a large
`noise_sd` MDE is larger or `None` — monotone sanity.

### Step 4: Budget guard in the run loop

In `run_experiment`'s `run_and_persist` (after `res` is obtained, under the existing
write lock or a small shared counter), accumulate cost and, once the ceiling is
crossed, stop launching/awaiting further runs. Minimal approach that respects the
existing `asyncio.gather`: keep a shared `{"spent": 0.0, "stop": False}`; in
`run_and_persist`, if `stop` is already set, return a `_failed_result(... "budget
ceiling reached")` WITHOUT calling `execute_run` (so no spend); else run, add
`res.cost_usd or 0`, and set `stop` when `cfg.cost_ceiling_usd` is crossed. Because
all jobs are already scheduled, the guard prevents the *expensive* `execute_run`
work for not-yet-started jobs rather than cancelling in-flight ones. Persist
everything completed (the JSONL append already happens per run).

**Verify**: a unit test (Step 6) drives the guard logic with a fake `execute_run`
that returns increasing costs and asserts it stops spending past the ceiling.
(Extract the guard into a small pure helper `_should_stop(spent, ceiling)` so it's
testable without `claude`.)

### Step 5: `plan` subcommand

```python
pp = sub.add_parser("plan", help="dry-run: project cost/time + MDE before spending")
pp.add_argument("-c", "--config", type=Path, default=Path("skillab.toml"))
pp.add_argument("--cost-per-run", type=float, default=None)
pp.add_argument("--seconds-per-run", type=float, default=None)
pp.add_argument("--baseline", type=float, default=None)
pp.add_argument("--noise", type=float, default=None)
```
handler: load cfg+tasks; `est = estimate_cost(cfg, tasks, args.cost_per_run, args.seconds_per_run)`;
print a human line ("projected: 20 runs, ~$6.20, ~12 min wall, ceiling $X"); if
`--baseline`/`--noise` given, also print `minimum_detectable_effect(...)` ("at k=5,
2 tasks you can detect ≥ +12% with 80% power"). If neither cost nor baseline is
known, print guidance to run one pilot or pass the flags. **`plan` spends nothing.**

**Verify**: `python3 skills_test.py plan -c <cfg> --cost-per-run 0.31
--seconds-per-run 40` prints a projection line with `$6.20` for a 2-task k=5 config.

### Step 6: Tests

```python
def test_estimate_cost_math():
    cfg = _cfg(k=5)  # judge_enabled defaults False
    tasks = [h.Task(id="a", prompt="x"), h.Task(id="b", prompt="y")]
    e = h.estimate_cost(cfg, tasks, per_run_usd=0.31, per_run_seconds=40.0)
    assert e["n_runs"] == 20 and e["n_judge_calls"] == 0
    assert round(e["projected_usd"], 2) == 6.20
    # judge on -> adds 2 * min(k,pairs) * tasks calls
    cfg2 = _cfg(k=5, judge_enabled=True, judge_max_pairs_per_task=3)
    e2 = h.estimate_cost(cfg2, tasks, per_run_usd=0.10)
    assert e2["n_judge_calls"] == 2 * 3 * 2 and round(e2["projected_usd"], 2) == round(0.10*(20+12), 2)

def test_should_stop_ceiling():
    assert h._should_stop(10.0, 5.0) is True
    assert h._should_stop(4.0, 5.0) is False
    assert h._should_stop(99.0, None) is False   # no ceiling -> never stop

def test_mde_monotone():
    cfg = _cfg(k=6)
    small = h.minimum_detectable_effect(cfg, n_tasks=3, baseline=0.5, noise_sd=0.02, sims=60)
    big = h.minimum_detectable_effect(cfg, n_tasks=3, baseline=0.5, noise_sd=0.40, sims=60)
    assert small is not None and (big is None or big >= small)
```

**Verify**: `python3 test_skills_test.py` → `N passed`; line-length `OK`.

## Test plan

- `test_estimate_cost_math` — run/judge counts and `$` projection, judge off and on.
- `test_should_stop_ceiling` — the budget guard's pure predicate, incl. `None` ceiling.
- `test_mde_monotone` — more noise ⇒ MDE no smaller (or unreachable). Keep `sims`
  low for speed; this asserts direction, not a precise number.

## Done criteria

- [ ] `python3 -m py_compile …` exits 0
- [ ] `estimate_cost` returns correct `n_runs`/`projected_usd` (test passes)
- [ ] `_should_stop` predicate correct incl. `None` ceiling
- [ ] `minimum_detectable_effect` runs and is monotone in noise (test passes)
- [ ] `plan` subcommand prints a projection and spends nothing
- [ ] budget guard stops launching new runs past `cost_ceiling_usd`, persists completed runs
- [ ] new tests pass; line-length `OK`
- [ ] `plans/README.md` row for 006 updated

## STOP conditions

- `run_experiment`'s job-scheduling structure differs from the excerpt (no
  `run_and_persist`, or not `asyncio.gather`) — STOP; the guard insertion point is wrong.
- Implementing the ceiling appears to require cancelling in-flight `asyncio` tasks
  mid-`claude` — STOP and report; the intended design only prevents *not-yet-started*
  runs from spending (cancelling a paid in-flight call wastes the spend anyway).
- MDE simulation is too slow (>~5s in tests) — reduce `sims`/bootstrap iters; it's a
  planning aid, not final inference.

## Maintenance notes

- The dry-run cost prior is only as good as `--cost-per-run`/the pilot. Document that
  the first real run calibrates it (`RunResult.cost_usd` is recorded) so the next
  `plan` can read a measured prior from `results.jsonl`.
- If `run_experiment` later switches to a streaming/`as_completed` scheduler, revisit
  the budget guard — the "prevent not-yet-started" semantics depend on jobs sharing one
  loop and a counter.
- Reviewer: confirm `plan` has no code path that calls `run_agent`/`execute_run` (it
  must never spend), and that the ceiling can't be bypassed by the judge phase.
