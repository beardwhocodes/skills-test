# Plan 016: Give the blind-judge win-rate a cluster-bootstrap confidence interval

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, the reviewer maintains the index
> (`plans/README.md`); do NOT edit it — your row is added for you.
>
> **Drift check (run first)**: this repo is NOT git-initialized, so there is no
> SHA to diff against. Before editing, compare the "Current state" excerpts
> below to the live code in `skills_test.py`. If any excerpt no longer
> matches the file (line numbers may shift slightly — match on the code text,
> not the line number), treat it as a STOP condition.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: LOW
- **Depends on**: none (touches the judge aggregation + report surfaces only)
- **Category**: direction
- **Planned at**: not git-initialized, 2026-06-25

## Why this matters

`CLAUDE.md` advertises (in "Open work", marked `[x]` DONE) that the blind judge
"reports a **win-rate + cluster-bootstrap CI** + position-consistency". That is
half false today: `aggregate_judge` returns only a **point** win-rate
(`win_rate_a = a_wins / decisive`) with no interval, and neither the Markdown
report nor the HTML judge panel shows one. A point win-rate of "0.75" over a
handful of judged diffs reads as far more settled than it is — the whole reason
the rest of this harness reports cluster-bootstrap CIs instead of point
estimates is to stop exactly that over-reading. This plan adds a
cluster-bootstrap CI for the judge win-rate (resample tasks, then judged
comparisons within task — the same structure the quantitative metrics already
use) and surfaces `[lo, hi]` in all three places, making the documented claim
true. The judge stays NON-DETERMINISTIC and clearly separated from the hard
scorers; this only quantifies its noise instead of hiding it.

## Current state

Single module under change: `/Users/copyjosh/Code/skills-test/skills_test.py`
(~3715 lines). Tests: `/Users/copyjosh/Code/skills-test/test_skills_test.py`
(custom stdlib runner — every top-level `def test_*` is auto-discovered and run;
53 pass today).

### The data unit being aggregated — `JudgeComparison` (`skills_test.py:1351-1360`)

```python
@dataclass
class JudgeComparison:
    task_id: str
    pair_id: int
    ordering: str               # "a_first" | "b_first"
    winner_arm: str | None      # the winning arm LABEL, "tie", or None (unparseable)
    reason: str = ""
    pair: str = ""              # comparison key, e.g. "skill-a_vs_skill-b"
    a_label: str = ""          # the two arm labels compared in this pair
    b_label: str = ""
```

Each comparison is one judge vote. `winner_arm` equals the pair's `a_label`,
its `b_label`, `"tie"`, or `None` (unparseable). Both orderings of the same
diff-pair are two separate rows.

### What `aggregate_judge` returns today — NO interval (`skills_test.py:1527-1556`)

```python
def aggregate_judge(comparisons: list[JudgeComparison]) -> dict:
    """Per-comparison-pair aggregation. Returns {pair_key: {a_label, b_label,
    a_wins, b_wins, ties, failed, decisive, win_rate_a, consistent, total_pairs}}.
    win_rate_a = a_label wins / decisive (0.5 = no preference between a and b)."""
    by_pair: dict[str, list[JudgeComparison]] = {}
    for c in comparisons:
        by_pair.setdefault(c.pair, []).append(c)
    out = {}
    for pkey, comps in by_pair.items():
        la, lb = comps[0].a_label, comps[0].b_label
        a_wins = sum(1 for c in comps if c.winner_arm == la)
        b_wins = sum(1 for c in comps if c.winner_arm == lb)
        ties = sum(1 for c in comps if c.winner_arm == "tie")
        failed = sum(1 for c in comps if c.winner_arm is None)
        decisive = a_wins + b_wins
        groups: dict[tuple[str, int], dict[str, str | None]] = {}
        for c in comps:
            groups.setdefault((c.task_id, c.pair_id), {})[c.ordering] = c.winner_arm
        consistent = total = 0
        for d in groups.values():
            af, bf = d.get("a_first"), d.get("b_first")
            if af is not None and bf is not None:   # both orderings produced a verdict
                total += 1
                if af == bf:
                    consistent += 1
        out[pkey] = {"a_label": la, "b_label": lb, "a_wins": a_wins, "b_wins": b_wins,
                     "ties": ties, "failed": failed, "decisive": decisive,
                     "win_rate_a": (a_wins / decisive if decisive else float("nan")),
                     "consistent": consistent, "total_pairs": total}
    return out
```

Note `win_rate_a` is `float("nan")` when `decisive == 0` (all ties/failed).

### The CI structure to MIRROR — `cluster_bootstrap_ci` (`skills_test.py:1103-1135`)

```python
def cluster_bootstrap_ci(on: dict[str, list[float]], off: dict[str, list[float]],
                         shared: list[str], iters: int, alpha: float,
                         rng: random.Random) -> tuple[float, float, bool]:
    """Resample TASKS with replacement, then runs within each sampled task (per
    arm). The shared task `picks` are drawn ONCE per iteration and used for BOTH
    arms, so between-task variance cancels in the difference (unpaired,
    task-clustered). Falls back to flat run-level resampling when <2 shared
    tasks; that path is anticonservative, flagged via the returned bool."""
    diffs: list[float] = []
    clustered = len(shared) >= 2

    if not clustered:                                # degenerate clustering
        flat_on = [v for t in shared for v in on[t]]
        flat_off = [v for t in shared for v in off[t]]
        if not flat_on or not flat_off:
            return (0.0, 0.0, False)
        for _ in range(iters):
            ro = [rng.choice(flat_on) for _ in flat_on]
            rf = [rng.choice(flat_off) for _ in flat_off]
            diffs.append(statistics.fmean(ro) - statistics.fmean(rf))
    else:
        for _ in range(iters):
            picks = [rng.choice(shared) for _ in shared]
            on_vals, off_vals = [], []
            for t in picks:
                on_vals += [rng.choice(on[t]) for _ in on[t]]
                off_vals += [rng.choice(off[t]) for _ in off[t]]
            diffs.append(statistics.fmean(on_vals) - statistics.fmean(off_vals))

    diffs.sort()
    lo = diffs[int((alpha / 2) * len(diffs))]
    hi = diffs[min(len(diffs) - 1, int((1 - alpha / 2) * len(diffs)))]
    return (lo, hi, clustered)
```

Copy the percentile indexing (`int((alpha / 2) * len(...))` and the `min(len-1,
...)` for the upper bound) **exactly** — it is the project's bootstrap
convention. `cfg.bootstrap_iters` (default 10_000; tests use 2000) and
`cfg.bootstrap_alpha` (default 0.05) are the standard knobs.

### The Markdown report — prints win rate + consistency, NO CI (`skills_test.py:1559-1604`)

```python
def build_judge_report(comparisons: list[JudgeComparison], cfg: ExperimentConfig,
                       results: list[RunResult] | None = None, seed: int = 0) -> str:
    banner = "## Qualitative — blind LLM judge (NON-DETERMINISTIC)"
    coverage = None
    ...
    for pkey, agg in aggregate_judge(comparisons).items():
        la, lb = agg["a_label"], agg["b_label"]
        wr = agg["win_rate_a"]
        wr_s = f"{wr:.3f}" if wr == wr else "n/a"
        cons = (f"{100 * agg['consistent'] / agg['total_pairs']:.0f}%"
                if agg["total_pairs"] else "n/a")
        verdict = "no decisive preference"
        if agg["decisive"]:
            if wr > 0.5:
                verdict = f"judge prefers **{la}**"
            elif wr < 0.5:
                verdict = f"judge prefers **{lb}**"
        lines += [
            "",
            f"### {la} vs {lb}",
            f"- **{la} win rate: {wr_s}** over {lb}  ({agg['a_wins']}–{agg['b_wins']}, "
            f"{agg['ties']} tie, {agg['failed']} unparseable; 0.5 = no preference)",
            f"- position-consistency: {cons} of pairs agreed across both orderings "
            f"(low = order-sensitive / noisy — discount)",
            f"- verdict: {verdict}",
        ]
    return "\n".join(lines)
```

The `seed: int = 0` parameter already exists on this function but is currently
**unused** — wiring it to seed the judge CI gives it a purpose. `wr == wr` is the
NaN check (NaN is the only value not equal to itself).

### The HTML chart data — emits win_rate_a, NO CI (`skills_test.py:2964-3010`)

```python
def _chart_data(results: list[RunResult], cfg: ExperimentConfig, metrics: list,
                pairs: list, pair_itt: dict, comparisons: list | None) -> dict:
    ...
    judge = []
    if comparisons:
        for pkey, agg in aggregate_judge(comparisons).items():
            wr = agg["win_rate_a"]
            judge.append({
                "pair": pkey, "a": agg["a_label"], "b": agg["b_label"],
                "a_wins": agg["a_wins"], "b_wins": agg["b_wins"], "ties": agg["ties"],
                "win_rate_a": (wr if wr == wr else None),
                "consistency": (round(100 * agg["consistent"] / agg["total_pairs"])
                                if agg["total_pairs"] else 0)})
    return {"metrics": metrics, "primary": cfg.primary_metric,
            ...
            "armMeans": arm_means, "comparisons": comps, "judge": judge}
```

### The HTML caller — `build_html_report` already has a seeded `rng` (`skills_test.py:3077-3107`)

```python
def build_html_report(results: list[RunResult], pf: Preflight, cfg: ExperimentConfig,
                      manifest: dict, comparisons: list | None = None,
                      seed: int = 0) -> str:
    rng = random.Random(seed)
    ...
    # Compute each (pair, metric) ITT estimate ONCE; reuse for charts AND audit.
    pair_itt: dict = {}
    for a, b in pairs:
        ...
    ...
    data = _chart_data(results, cfg, metrics, pairs, pair_itt, comparisons)
```

`rng` is created at the top and is already consumed by `estimate_diff` for the
ITT estimates (computed in the `pair_itt` loop) BEFORE the `_chart_data` call.
That ordering matters: passing the SAME `rng` into `_chart_data` afterward does
not perturb any ITT number (those are already computed), and keeps the whole
report deterministic for a given `seed`.

### The HTML judge panel JS — renders win_rate_a, NO CI (`skills_test.py:2778-2846`)

This lives inside `_HTML_SCRIPT`, a **raw triple-quoted r-string** of ES5 vanilla
JS (`_HTML_SCRIPT = r"""(function(){` at `skills_test.py:2201`). Convention:
`var`/`function` only, **no** template literals, **no** backticks, lines stay
**< 100 columns**. Relevant excerpts:

```javascript
  function judgeSection(){
    var J = D.judge || [];
    if (!J.length) return "";
    ...
    var rows = J.map(function(j){
      var low = j.consistency <= 50, favA = j.win_rate_a >= 0.5;
      ...
      var t = tip(esc(j.a) + " vs " + esc(j.b), [
        ["win-rate (A):", j.win_rate_a.toFixed(2)],
        ["record:", j.a_wins + "–" + j.b_wins + "–" + j.ties
          + " (A–B–tie)"],
        ["consistency:", j.consistency + "%" + (low ? " (≈ chance)" : "")]
      ]);
      ...
      return "<div class='jrow'><div class='jlabel'>...</div><div class='jmeta'>"
        + "<div class='wr num'>" + j.win_rate_a.toFixed(2) + " <span style='"
        + "font-size:11px;font-weight:500;color:var(--faint)'>win-rate A</span></div>"
        + "<div class='num'>record " + j.a_wins + "–" + j.b_wins
        + (j.ties ? "–" + j.ties : "") + "</div><div style='margin-top:5px'>"
        + consHtml + "</div></div></div>";
    }).join("");
    ...
  }
```

(The `return` string above is abbreviated — the `<div class='jrow'>...` prefix is
long; match on the `jmeta` / `wr num` portion, which is where you insert.) Note
`j.win_rate_a.toFixed(2)` already assumes `win_rate_a` is non-null; the all-ties
`null` case is a SEPARATE pre-existing crash owned by **plan 007** — do not fix
it here. Guard your NEW CI access so you do not add a second null crash.

### Conventions that apply

- Comments explain WHY, not what.
- STDLIB ONLY — Python >= 3.11. **NEVER add a dependency** (no numpy/pandas/
  pytest/etc.). The bootstrap is hand-rolled with `random` + list slicing, exactly
  like `cluster_bootstrap_ci`.
- Ruff line-length is 100 for both the Python and the JS-inside-strings.
- Test helpers to reuse: `_cmp(task, pid, ordering, winner, a=, b=, pair=)` builds
  a `JudgeComparison` (`test_skills_test.py:240-243`); `_cfg(**kw)` builds an
  `ExperimentConfig` with `bootstrap_iters=2000` by default
  (`test_skills_test.py:18-25`).

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Tests | `python3 test_skills_test.py` (run from repo root) | ends with `N passed`, exit 0 |
| Lint | `uvx ruff check skills_test.py` | `All checks passed!`, exit 0 |
| Quick probe | `python3 -c "import skills_test as h; ..."` | per-step expected print |

Run all commands from `/Users/copyjosh/Code/skills-test`. No `claude`, `git`, or
network is needed — every change is pure Python/JS-string logic and the tests are
offline.

## Scope

**In scope** (the only files you should modify):
- `/Users/copyjosh/Code/skills-test/skills_test.py` — add the CI helper,
  thread it through `aggregate_judge`, `build_judge_report`, `_chart_data` (+ its
  `build_html_report` caller), and the `judgeSection` JS.
- `/Users/copyjosh/Code/skills-test/test_skills_test.py` — new tests.

**Out of scope** (do NOT touch, even though they look related):
- `/Users/copyjosh/Code/skills-test/CLAUDE.md` — the "Open work" bullet already
  reads "win-rate + cluster-bootstrap CI + position-consistency", so once this
  plan lands the claim is already TRUE. Do NOT edit it. Only if, after your
  change, that exact wording is still inaccurate (it should not be) may you
  correct it — and if so, change ONLY that one bullet. Default: leave CLAUDE.md
  untouched.
- The null-`win_rate_a` HTML crash (all-ties case where `win_rate_a` is `null`) —
  owned by **plan 007** (`plans/007-judge-null-winrate-dashboard-crash.md`). Do
  not change the existing `j.win_rate_a.toFixed(2)` calls; only ADD guarded CI
  rendering.
- `cluster_bootstrap_ci`, `cluster_permutation_p`, `estimate_diff` and any
  quantitative-metric path — the judge CI is a separate, parallel helper.
- `plans/README.md` — the reviewer maintains it.

## Git workflow

This repo is NOT git-initialized. There is no branch to cut and no commit to
make — edit the two files in place. Do not run `git` commands.

## Steps

### Step 1: Add the `judge_winrate_ci` helper

Insert a new function in `skills_test.py` immediately ABOVE `def
aggregate_judge` (i.e. just before `skills_test.py:1527`). It must mirror
`cluster_bootstrap_ci`'s task-then-within-task resampling and percentile indexing,
but recompute the judge win-rate each iteration. Target shape:

```python
def judge_winrate_ci(comps: list[JudgeComparison], iters: int, alpha: float,
                     rng: random.Random) -> tuple[float, float] | None:
    """Cluster-bootstrap CI for one pair's win_rate_a. Mirrors cluster_bootstrap_ci:
    resample TASKS with replacement, then judged comparisons within each sampled
    task, recomputing win_rate_a = a_wins / decisive per iteration. Resamples whose
    decisive count is 0 (all ties/failed) are skipped; returns None if no resample
    is ever decisive (e.g. every comparison was a tie) so callers can show no CI
    instead of dividing by zero."""
    if not comps:
        return None
    la, lb = comps[0].a_label, comps[0].b_label
    by_task: dict[str, list[JudgeComparison]] = {}
    for c in comps:
        by_task.setdefault(c.task_id, []).append(c)
    tasks = list(by_task)
    rates: list[float] = []
    for _ in range(iters):
        picks = [rng.choice(tasks) for _ in tasks]
        a_wins = b_wins = 0
        for t in picks:
            rows = by_task[t]
            for _ in rows:
                w = rng.choice(rows).winner_arm
                if w == la:
                    a_wins += 1
                elif w == lb:
                    b_wins += 1
        dec = a_wins + b_wins
        if dec:
            rates.append(a_wins / dec)
    if not rates:
        return None
    rates.sort()
    lo = rates[int((alpha / 2) * len(rates))]
    hi = rates[min(len(rates) - 1, int((1 - alpha / 2) * len(rates)))]
    return (lo, hi)
```

WHY skip `dec == 0` resamples rather than treat them as 0.5: a resample that
drew only ties/failures carries no preference signal; counting it as 0.5 would
fabricate a "no preference" data point and bias the interval toward the center.
Skipping is the honest analogue of `cluster_bootstrap_ci`'s empty-arm guard.

**Verify**: `python3 -c "import skills_test as h, random; print(h.judge_winrate_ci([], 100, 0.05, random.Random(0)))"`
→ prints `None`.

### Step 2: Thread the CI through `aggregate_judge`

Change the signature at `skills_test.py:1527` to accept keyword-only,
defaulted CI knobs so existing callers (`aggregate_judge(comparisons)`) keep
working and simply get `win_rate_a_ci: None`:

```python
def aggregate_judge(comparisons: list[JudgeComparison], *, ci_iters: int = 0,
                    alpha: float = 0.05, rng: random.Random | None = None) -> dict:
```

Inside the per-pair loop, before building `out[pkey]`, compute the CI only when
enabled (a positive `ci_iters` AND an `rng`):

```python
        ci = (judge_winrate_ci(comps, ci_iters, alpha, rng)
              if ci_iters and rng is not None else None)
```

Add `"win_rate_a_ci": ci,` as a new key in the `out[pkey] = {...}` dict. Update
the docstring's returned-keys list to include `win_rate_a_ci`.

**Verify**: `python3 -c "import skills_test as h, random; c=[h.JudgeComparison('t',0,'a_first','my-skill',pair='p',a_label='my-skill',b_label='control'),h.JudgeComparison('t',1,'a_first','my-skill',pair='p',a_label='my-skill',b_label='control')]; a=h.aggregate_judge(c, ci_iters=500, alpha=0.05, rng=random.Random(0))['p']; print(a['win_rate_a'], a['win_rate_a_ci']); print(h.aggregate_judge(c)['p']['win_rate_a_ci'])"`
→ first line prints `1.0 (1.0, 1.0)`; second line prints `None`.

### Step 3: Show the CI in the Markdown report

In `build_judge_report` (`skills_test.py:1583`), change the aggregation call
to seed and enable the CI using the function's existing `seed` parameter and the
config's bootstrap knobs:

```python
    for pkey, agg in aggregate_judge(
            comparisons, ci_iters=cfg.bootstrap_iters,
            alpha=cfg.bootstrap_alpha, rng=random.Random(seed)).items():
```

Inside that loop, after `wr_s = ...`, add a CI string:

```python
        ci = agg["win_rate_a_ci"]
        ci_s = f" [{ci[0]:.3f}, {ci[1]:.3f}]" if ci else ""
```

Then append `{ci_s}` to the win-rate line so it reads (note `{ci_s}` goes right
after `{wr_s}`, still inside the bold span):

```python
            f"- **{la} win rate: {wr_s}{ci_s}** over {lb}  ({agg['a_wins']}–{agg['b_wins']}, "
```

Leave the `position-consistency` and `verdict` lines unchanged. The substring
`win rate: {wr_s}` is preserved (e.g. `win rate: 1.000 [1.000, 1.000]`), so the
existing `test_build_judge_report_empty_and_basic` assertion still holds.

**Verify**: `python3 test_skills_test.py` → still `53 passed` (no behavior
asserted on the new text yet; new tests come in Step 6).

### Step 4: Emit the CI in `_chart_data`

`_chart_data` needs an `rng` to compute the CI deterministically. Add it as a new
parameter (`skills_test.py:2964`):

```python
def _chart_data(results: list[RunResult], cfg: ExperimentConfig, metrics: list,
                pairs: list, pair_itt: dict, comparisons: list | None,
                rng: random.Random) -> dict:
```

Change the judge aggregation call (`skills_test.py:2998`) to enable the CI,
and add the new key to each judge entry:

```python
        for pkey, agg in aggregate_judge(
                comparisons, ci_iters=cfg.bootstrap_iters,
                alpha=cfg.bootstrap_alpha, rng=rng).items():
            wr = agg["win_rate_a"]
            judge.append({
                "pair": pkey, "a": agg["a_label"], "b": agg["b_label"],
                "a_wins": agg["a_wins"], "b_wins": agg["b_wins"], "ties": agg["ties"],
                "win_rate_a": (wr if wr == wr else None),
                "win_rate_a_ci": agg["win_rate_a_ci"],
                "consistency": (round(100 * agg["consistent"] / agg["total_pairs"])
                                if agg["total_pairs"] else 0)})
```

Update the single caller in `build_html_report` (`skills_test.py:3107`) to
pass the already-existing `rng`:

```python
    data = _chart_data(results, cfg, metrics, pairs, pair_itt, comparisons, rng)
```

WHY pass the existing `rng` (not a fresh one): the ITT estimates in `pair_itt`
are already computed above this line, so consuming `rng` here cannot change them,
and reusing the seeded generator keeps the whole report reproducible for a given
`seed`.

**Verify**: `python3 -c "import skills_test as h; import inspect; print('rng' in inspect.signature(h._chart_data).parameters)"`
→ prints `True`. Then `python3 test_skills_test.py` → still `53 passed`.

### Step 5: Render the CI in the `judgeSection` JS panel

Edit the ES5 JS inside `_HTML_SCRIPT` (`skills_test.py:2778-2846`). ES5 only:
`var`/`function`, no template literals/backticks, every line < 100 columns. Guard
all CI access with `j.win_rate_a_ci ?` (it is `null` when there is no CI).

(a) Add a CI row to the hover tooltip array (after the `"win-rate (A):"` row):

```javascript
        ["95% CI (A):", j.win_rate_a_ci ? "[" + j.win_rate_a_ci[0].toFixed(2)
          + ", " + j.win_rate_a_ci[1].toFixed(2) + "]" : "n/a"],
```

(b) Add a CI readout to the right-hand `jmeta` block. Insert it right after the
`<div class='wr num'>...win-rate A</span></div>` fragment and before the
`<div class='num'>record ...` fragment:

```javascript
        + (j.win_rate_a_ci ? "<div class='num' style='font-size:11px;color:"
          + "var(--faint)'>95% CI [" + j.win_rate_a_ci[0].toFixed(2) + ", "
          + j.win_rate_a_ci[1].toFixed(2) + "]</div>" : "")
```

Do NOT alter the existing `j.win_rate_a.toFixed(2)` calls or the SVG bar geometry
(drawing a CI whisker on the bar is explicitly out of scope for this plan).

**Verify**: `uvx ruff check skills_test.py` → `All checks passed!` (ruff
also flags any Python line that drifted over 100 cols while editing the string).
Then `python3 -c "import skills_test as h; assert 'win_rate_a_ci' in h._HTML_SCRIPT; assert '95% CI' in h._HTML_SCRIPT; print('ok')"`
→ prints `ok`.

### Step 6: Add tests

Add these top-level `def test_*` functions to `test_skills_test.py` (the
runner auto-discovers them). Place them next to the other judge tests (near
`test_aggregate_judge_double_failure_not_consistent`, ~line 578). Reuse the
existing `_cmp(...)` helper (`test_skills_test.py:240-243`) and `random`
(already imported at the top of the test file).

```python
def test_judge_winrate_ci_brackets_point():
    # 3 my-skill wins + 1 control win across 2 tasks -> win_rate_a = 0.75; the
    # cluster-bootstrap CI must bracket the point estimate and stay in [0, 1].
    import random as _r
    comps = [_cmp("t1", 0, "a_first", "my-skill"), _cmp("t1", 1, "a_first", "my-skill"),
             _cmp("t2", 0, "a_first", "my-skill"), _cmp("t2", 1, "a_first", "control")]
    agg = h.aggregate_judge(comps, ci_iters=2000, alpha=0.05,
                            rng=_r.Random(0))["my-skill_vs_control"]
    assert agg["win_rate_a"] == 0.75              # point estimate unchanged
    ci = agg["win_rate_a_ci"]
    assert isinstance(ci, tuple) and len(ci) == 2
    lo, hi = ci
    assert 0.0 <= lo <= agg["win_rate_a"] <= hi <= 1.0


def test_judge_winrate_ci_none_when_all_ties():
    # decisive == 0 (every vote a tie) -> win_rate_a is nan and the CI is None,
    # with no ZeroDivisionError (degenerate case; ties into plan 007).
    import random as _r
    comps = [_cmp("t", 0, "a_first", "tie"), _cmp("t", 0, "b_first", "tie")]
    agg = h.aggregate_judge(comps, ci_iters=500, alpha=0.05,
                            rng=_r.Random(0))["my-skill_vs_control"]
    assert agg["win_rate_a"] != agg["win_rate_a"]     # nan
    assert agg["win_rate_a_ci"] is None


def test_aggregate_judge_default_has_no_ci():
    # Backward compat: callers that don't ask for a CI get win_rate_a_ci == None.
    comps = [_cmp("t", 0, "a_first", "my-skill"), _cmp("t", 0, "b_first", "my-skill")]
    assert h.aggregate_judge(comps)["my-skill_vs_control"]["win_rate_a_ci"] is None


def test_judge_report_shows_ci():
    # A clean sweep (win_rate 1.0 over two tasks) -> CI [1.000, 1.000] in the text.
    cfg = _cfg(bootstrap_iters=200)
    comps = [_cmp("a", 0, "a_first", "my-skill"), _cmp("a", 0, "b_first", "my-skill"),
             _cmp("b", 0, "a_first", "my-skill"), _cmp("b", 0, "b_first", "my-skill")]
    rep = h.build_judge_report(comps, cfg)
    assert "win rate: 1.000 [1.000, 1.000]" in rep
```

**Verify**: `python3 test_skills_test.py` → ends with `57 passed` (53 +
4 new), exit 0.

### Step 7: Final gate

Run both quality gates.

**Verify**:
- `python3 test_skills_test.py` → `57 passed`, exit 0.
- `uvx ruff check skills_test.py` → `All checks passed!`, exit 0.

## Test plan

New tests in `test_skills_test.py` (model their style on the existing judge
tests `test_aggregate_position_bias_washes_out` and
`test_aggregate_judge_double_failure_not_consistent`):

- `test_judge_winrate_ci_brackets_point` — happy path: `win_rate_a` is unchanged
  by adding the CI, and the returned `[lo, hi]` satisfies `0 <= lo <= win_rate_a
  <= hi <= 1` (the core invariant the plan exists to add).
- `test_judge_winrate_ci_none_when_all_ties` — degenerate `decisive == 0` edge:
  CI is `None` and nothing divides by zero.
- `test_aggregate_judge_default_has_no_ci` — backward-compat edge: omitting the
  CI args yields `win_rate_a_ci is None` (existing callers unaffected).
- `test_judge_report_shows_ci` — end-to-end through `build_judge_report` (the real
  caller, exercising the `seed`→`rng`→`aggregate_judge` wiring, not a mock): the
  `[lo, hi]` text appears in the report.

Verification: `python3 test_skills_test.py` → all pass, including the 4 new
tests (total `57 passed`).

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `python3 test_skills_test.py` exits 0 and prints `57 passed` (the 4 new
      judge-CI tests exist and pass alongside the original 53).
- [ ] `uvx ruff check skills_test.py` prints `All checks passed!`, exit 0.
- [ ] `python3 -c "import skills_test as h, inspect; assert 'win_rate_a_ci' in h.aggregate_judge([]).__class__ or True; assert hasattr(h, 'judge_winrate_ci'); assert 'rng' in inspect.signature(h._chart_data).parameters; print('ok')"`
      prints `ok` (helper exists; `_chart_data` takes `rng`).
- [ ] `python3 -c "import skills_test as h; assert '95% CI' in h._HTML_SCRIPT and 'win_rate_a_ci' in h._HTML_SCRIPT; print('ok')"`
      prints `ok` (HTML panel renders the CI).
- [ ] No dependency added — `grep -nE '^(import|from) ' skills_test.py` shows
      only stdlib modules (e.g. `asyncio`, `random`, `statistics`, `hashlib`,
      `json`, `subprocess`, `dataclasses`, `pathlib`); nothing new.
- [ ] Only `skills_test.py` and `test_skills_test.py` were modified;
      `CLAUDE.md` is unchanged.

## STOP conditions

Stop and report back (do not improvise) if:

- The code at any location in "Current state" does not match the excerpts above
  (the module drifted since this plan was written) — especially if
  `aggregate_judge`, `build_judge_report`, `_chart_data`, `build_html_report`, or
  the `judgeSection` JS already reference a CI / `win_rate_a_ci`. If a CI already
  exists, this plan is partly done; report what is present instead of duplicating.
- `build_html_report` no longer has a seeded `rng` in scope at the `_chart_data`
  call site (Step 4's wiring assumption is false).
- A verification command fails twice after a reasonable fix attempt.
- Implementing the CI appears to require touching an out-of-scope file (e.g.
  `cluster_bootstrap_ci`, or `CLAUDE.md` beyond the single judge bullet).
- The `test_judge_winrate_ci_brackets_point` invariant `lo <= win_rate_a <= hi`
  fails for `random.Random(0)` — that signals the resampling logic diverged from
  the intended task-then-within-task structure; do NOT just loosen the assertion.

## Maintenance notes

For the owner of this code after the change lands:

- The judge CI deliberately resamples **individual `JudgeComparison` rows** within
  a task (each ordering vote is its own unit), matching the prompt's
  "tasks, then judged comparisons within task" structure and
  `cluster_bootstrap_ci`'s run-within-task analogue. If a future change wants to
  respect the correlation between the two orderings of the same diff-pair, switch
  the within-task resample unit to `(task_id, pair_id)` groups — that is a finer
  cluster and a deliberate, separately-justified choice, not a bug.
- Single-task judge data gives an anticonservative (narrow) interval, exactly like
  `cluster_bootstrap_ci`'s `<2 shared tasks` fallback. This plan does NOT add a
  `clustered` flag to the judge CI because the judge section is already blanket-
  labelled NON-DETERMINISTIC; if a reviewer wants parity with the quantitative
  badge's single-task warning, that is a follow-up.
- The all-ties `null win_rate_a` HTML render crash is still owned by **plan 007**;
  this plan only guards its OWN new CI rendering against `null`. When 007 lands,
  re-check that the CI div and the win-rate div degrade together.
- `CLAUDE.md`'s "Open work" judge bullet should now be accurate end-to-end
  ("win-rate + cluster-bootstrap CI + position-consistency"). A reviewer should
  confirm the claim matches the shipped behavior rather than re-marking it.
- What a reviewer should scrutinize in the diff: that the percentile indexing was
  copied verbatim from `cluster_bootstrap_ci` (off-by-one in the bounds is the
  classic bootstrap bug), and that the JS stays ES5 / < 100 cols inside the raw
  string.
- Index note: the reviewer adds this plan's row to `plans/README.md`; you do not.
