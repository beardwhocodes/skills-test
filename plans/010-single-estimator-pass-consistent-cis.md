# Plan 010: Compute every pairwise estimate ONCE so markdown == JSON == HTML == badge

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. Your status row in `plans/README.md` is maintained
> by the reviewer who dispatched you — do NOT edit `plans/README.md`.
>
> **Drift check (run first)**: this repo is NOT git-initialized, so there is no
> SHA to diff against. Before editing, open `skills_test.py` and compare
> each "Current state" excerpt below (quoted with `file:line`) to the live code.
> If any excerpt no longer matches (line numbers may shift; the *code* must
> match), treat it as a STOP condition and report the mismatch.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: LOW–MED
- **Depends on**: plans/009-*.md (land its HTML-blob test helpers first so this
  plan's equality assertion has a stable extraction path). If plan 009 is not yet
  DONE, you may still proceed — this plan defines its own blob extraction inline.
- **Category**: tech-debt (correctness + performance)
- **Planned at**: not git-initialized, 2026-06-25
- **Issue**: —

## Why this matters

The same `(pair, metric)` cluster bootstrap + permutation test is recomputed by
THREE independent renderers — the markdown report (`build_report`), the JSON
summary (`summary_dict` via `_pair_estimates`), and the HTML dashboard
(`build_html_report`). Each renderer makes its own `random.Random(seed)` and
calls `estimate_diff` a DIFFERENT number of times in a DIFFERENT order before it
reaches any given pair, so the shared RNG is at a different position for the
second and third pairs. Result: for non-first pairs the bootstrap draws diverge
and the **confidence intervals reported for the same run disagree between
outputs**.

This is not cosmetic. In a 3-arm head-to-head the `experiment_pairs` order is
`[(on,off), (b,off), (on,b)]` and `primary_pair` is `(on,b)` — i.e. the PRIMARY
comparison is pair #3. The badge/CI gate reads its CI from `summary_dict`'s
estimate of pair #3; the HTML hero reads its CI from `build_html_report`'s own
estimate of pair #3. Because the two renderers consumed the RNG differently
before reaching pair #3, **the badge's headline CI and the dashboard's headline
CI can disagree for the identical run.** On top of the correctness bug, the work
is done 2–3× over (each `estimate_diff` runs 10k bootstrap + 10k permutation
iterations).

After this plan: one function computes each `(pair, mode, metric)` estimate
exactly once with a single seeded RNG; all three renderers and the badge render
from that one structure, so their numbers are bit-identical and the redundant
bootstrap passes are gone.

## Current state

Single module: `/Users/copyjosh/Code/skills-test/skills_test.py` (~3715
lines). Tests: `/Users/copyjosh/Code/skills-test/test_skills_test.py`
(custom stdlib runner — NOT pytest; 53 tests currently pass in <1s; no
claude/git/network needed).

### The shared currency — `DiffEstimate` (do NOT change its fields)

`skills_test.py:1069-1081`:

```python
class DiffEstimate:
    metric: str
    mean_on: float
    mean_off: float
    point: float          # mean_on - mean_off, over the SHARED task set
    ci_low: float
    ci_high: float
    p_value: float        # cluster permutation, null-calibrated
    n_on: int
    n_off: int
    n_tasks: int          # shared tasks (the basis for point, CI AND p)
    clustered: bool       # False => degenerate flat fallback (anticonservative)
    q_value: float | None = None   # BH-adjusted, secondary metrics only
```

### The estimator — consumes the RNG (each call = 10k bootstrap + 10k permutation)

`skills_test.py:1180-1200`:

```python
def estimate_diff(results: list[RunResult], metric: str, mode: str,
                  cfg: ExperimentConfig, rng: random.Random,
                  arm_a: Arm = Arm.SKILL_ON, arm_b: Arm = Arm.SKILL_OFF) -> DiffEstimate | None:
    ...
    on = _grouped(results, arm_a, metric, mode)
    off = _grouped(results, arm_b, metric, mode)
    shared = [t for t in on if t in off and on[t] and off[t]]
    if not shared:
        return None
    on_flat = [v for t in shared for v in on[t]]
    off_flat = [v for t in shared for v in off[t]]
    m_on, m_off = statistics.fmean(on_flat), statistics.fmean(off_flat)
    point = m_on - m_off
    lo, hi, clustered = cluster_bootstrap_ci(
        on, off, shared, cfg.bootstrap_iters, cfg.bootstrap_alpha, rng)
    p = cluster_permutation_p(on, off, shared, point, cfg.permutation_iters, rng)
    return DiffEstimate(metric, m_on, m_off, point, lo, hi, p,
                        len(on_flat), len(off_flat), len(shared), clustered)
```

`point`, `mean_on`, `mean_off`, `n_*`, `clustered` are RNG-INDEPENDENT (so
existing tests asserting `point` stay green). Only `ci_low`, `ci_high`,
`p_value` (and therefore the BH `q_value` derived from p) depend on the RNG
stream — that is exactly what diverges today.

### Arm/pair helpers (the iteration order that makes the bug 3-arm-specific)

`skills_test.py:219-235`:

```python
def experiment_pairs(cfg: ExperimentConfig) -> list[tuple[Arm, Arm]]:
    """Ordered (treatment, reference) pairs to estimate. 2-arm: skill vs control.
    3-arm: each skill vs control, then A vs B (the head-to-head)."""
    if _is_head_to_head(cfg):
        return [(Arm.SKILL_ON, Arm.SKILL_OFF), (Arm.SKILL_B, Arm.SKILL_OFF),
                (Arm.SKILL_ON, Arm.SKILL_B)]
    return [(Arm.SKILL_ON, Arm.SKILL_OFF)]


def primary_pair(cfg: ExperimentConfig) -> tuple[Arm, Arm]:
    """The headline comparison for the badge. Head-to-head -> A vs B; else the
    skill vs control."""
    return (Arm.SKILL_ON, Arm.SKILL_B) if _is_head_to_head(cfg) else (Arm.SKILL_ON, Arm.SKILL_OFF)


def pair_key(cfg: ExperimentConfig, a: Arm, b: Arm) -> str:
    return f"{arm_label(cfg, a)}_vs_{arm_label(cfg, b)}"
```

### Call site #1 — markdown `build_report` (ITT primary, ITT secondary, THEN pp; own rng)

`skills_test.py:1240` makes the rng; `1301-1322` is the per-pair loop:

```python
    rng = random.Random(seed)
```

```python
    for a, b in pairs:
        la, lb = arm_label(cfg, a), arm_label(cfg, b)
        ph = (f"| metric | {la} | {lb} | delta | 95% CI | p | q | tasks | dir |\n"
              "|---|---|---|---|---|---|---|---|---|")
        L += ["", f"## {la} − {lb} (ITT)",
              f"_primary endpoint `{primary}` (pre-registered, uncorrected); secondary "
              f"metrics Benjamini–Hochberg corrected_", ph]
        est = estimate_diff(results, primary, "itt", cfg, rng, a, b)
        L.append(_row(est, dir_map, alpha, use_q=False) if est
                 else f"| {primary} | — | — | (no shared tasks with valid runs) | | | | |")
        secondary = [m for m in metrics if m != primary]
        sec_ests = [(m, estimate_diff(results, m, "itt", cfg, rng, a, b)) for m in secondary]
        sec_ests = [(m, e) for m, e in sec_ests if e]
        for (_, e), q in zip(sec_ests, benjamini_hochberg([e.p_value for _, e in sec_ests])):
            e.q_value = q
        for _, e in sec_ests:
            L.append(_row(e, dir_map, alpha, use_q=True))
        pp_rows = [_row(e, dir_map, alpha, use_q=False) for m in metrics
                   if (e := estimate_diff(results, m, "pp", cfg, rng, a, b))]
        if pp_rows:
            L += ["", "<details><summary>per-protocol (conditioned on activation — "
                  "biased)</summary>", "", ph] + pp_rows + ["</details>"]
```

Per pair this consumes: itt(primary), itt(each secondary that has data), pp(each
metric that has data). The `_row` renderer reads only `DiffEstimate` attributes
(`skills_test.py:1215-1231`), so it can render straight from shared
`DiffEstimate` objects unchanged.

### Call site #2 — JSON `summary_dict` / `_pair_estimates` (ITT all metrics, THEN pp; own rng)

`skills_test.py:1654-1671`:

```python
def _pair_estimates(results: list[RunResult], cfg: ExperimentConfig, metrics: list,
                    rng: random.Random, arm_a: Arm, arm_b: Arm) -> dict:
    """ITT + per-protocol estimates for one (arm_a − arm_b) comparison, with
    Benjamini-Hochberg over the secondary metrics (primary stays uncorrected)."""
    def estimates(mode: str) -> dict:
        out = {}
        for m in metrics:
            e = estimate_diff(results, m, mode, cfg, rng, arm_a, arm_b)
            if e:
                out[m] = {k: getattr(e, k) for k in (
                    "mean_on", "mean_off", "point", "ci_low", "ci_high",
                    "p_value", "q_value", "n_on", "n_off", "n_tasks", "clustered")}
        return out
    itt = estimates("itt")
    secondary = [m for m in itt if m != cfg.primary_metric]
    for m, q in zip(secondary, benjamini_hochberg([itt[m]["p_value"] for m in secondary])):
        itt[m]["q_value"] = q
    return {"itt": itt, "per_protocol": estimates("pp")}
```

`skills_test.py:1674-1715` — `summary_dict` builds `comparisons` from
`_pair_estimates`, then exposes a convenience mirror of the PRIMARY pair that
the badge/CI read. The exact emitted shape MUST be preserved:

```python
def summary_dict(results: list[RunResult], cfg: ExperimentConfig, manifest: dict,
                 scorers: list[Scorer] | None = None, seed: int = 0) -> dict:
    ...
    scorers = scorers or default_scorers(cfg)
    rng = random.Random(seed)
    metrics = [s.name for s in scorers] + ["cost_usd"]

    comparisons = {}
    for a, b in experiment_pairs(cfg):
        comparisons[pair_key(cfg, a, b)] = {
            "a": arm_label(cfg, a), "b": arm_label(cfg, b),
            **_pair_estimates(results, cfg, metrics, rng, a, b)}
    ...
    pa, pb = primary_pair(cfg)
    pkey = pair_key(cfg, pa, pb)
    return {
        "schema_version": 2,
        "manifest": manifest,
        "primary_metric": cfg.primary_metric,
        "primary_pair": pkey,
        "arms": arm_stats,
        "comparisons": comparisons,
        # convenience mirror of the PRIMARY comparison (badge / simple consumers):
        "itt": comparisons[pkey]["itt"],
        "per_protocol": comparisons[pkey]["per_protocol"],
        "validity": {
            "on_valid": arm_stats[arm_label(cfg, pa)]["valid"],
            "off_valid": arm_stats[arm_label(cfg, pb)]["valid"],
            "off_contaminated": sum(1 for r in results if r.contaminated),
        },
    }
```

Note this emits a **field dict per metric** (the 11-key `getattr` set above),
NOT the `DiffEstimate` object, and the per-comparison key is **`per_protocol`**
(not `pp`). Both must stay.

### Call site #3 — HTML `build_html_report` (ITT all metrics ONLY — no pp; own rng)

`skills_test.py:3085` makes the rng; `3091-3104` computes + reads it:

```python
    rng = random.Random(seed)
    metrics = [s.name for s in scorers] + ["cost_usd"]
    ...
    # Compute each (pair, metric) ITT estimate ONCE; reuse for charts AND audit.
    pair_itt: dict = {}
    for a, b in pairs:
        key = pair_key(cfg, a, b)
        pair_itt[key] = {m: estimate_diff(results, m, "itt", cfg, rng, a, b) for m in metrics}
        sec = [m for m in metrics if m != cfg.primary_metric and pair_itt[key][m]]
        for m, q in zip(sec, benjamini_hochberg([pair_itt[key][m].p_value for m in sec])):
            pair_itt[key][m].q_value = q

    pa, pb = primary_pair(cfg)
    pe = pair_itt[pair_key(cfg, pa, pb)][cfg.primary_metric]
    verdict = (badge_verdict({"ci_low": pe.ci_low, "ci_high": pe.ci_high, "point": pe.point},
                             _metric_direction(cfg, cfg.primary_metric), pe.n_tasks,
                             pe.clustered, total_contam) if pe else None)
```

`pair_itt` here is `{pair_key: {metric: DiffEstimate|None}}` (objects, NOT field
dicts). It is consumed by `_chart_data(results, cfg, metrics, pairs, pair_itt,
comparisons)` (`skills_test.py:2964-2995`), which reads `e.point`,
`e.ci_low`, `e.ci_high`, `e.p_value`, `e.q_value` off each `DiffEstimate` and
emits the in-page blob as `itt[m] = {"point", "lo", "hi", "p", "q"}`. Note the
HTML JSON uses the keys `lo`/`hi`, while the summary JSON uses `ci_low`/`ci_high`
for the same numbers — that is why the regression test below compares
`summ["ci_low"]` against `chart["lo"]`.

The crucial asymmetry: site #1 consumes itt+pp per pair, site #2 consumes itt+pp
per pair (different itt order than #1 because #1 pulls the primary out first),
site #3 consumes itt ONLY. So all three RNGs are at different offsets when they
reach pair #2 and pair #3 → divergent CIs for those pairs.

### The badge / CI consumers that read `summary["itt"]` (do NOT break these)

`skills_test.py:1820-1827`:

```python
def primary_verdict(summary: dict, cfg: ExperimentConfig) -> dict | None:
    """The badge_verdict for the configured primary metric, or None."""
    metric = summary["primary_metric"]
    est = summary["itt"].get(metric)
    if not est:
        return None
    return badge_verdict(est, _metric_direction(cfg, metric), est["n_tasks"],
                         est["clustered"], summary["validity"]["off_contaminated"])
```

`badge_verdict` (`:1768-1783`) reads `est["ci_low"]`, `est["ci_high"]`,
`est["point"]`; `primary_verdict` additionally reads `est["n_tasks"]` and
`est["clustered"]`. `ci_exit_code` (`:3239-3255`) and `cmd_badge` go through
`primary_verdict`. So the field-dict keys `ci_low`, `ci_high`, `point`,
`n_tasks`, `clustered` are LOAD-BEARING and must survive in `summary["itt"]`.

### How the three are wired together (same seed in practice)

`_run_and_outputs` (`skills_test.py:3332-3350`) calls `build_report`,
`summary_dict`, and `build_html_report` all with the DEFAULT `seed=0`. `cmd_demo`
(`:3408-3411`) and the `report` command also default `seed=0`. Each function
currently makes its OWN `random.Random(seed)`, so equality between outputs
depends ONLY on identical within-function call order — which is what this plan
unifies.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Tests | `python3 test_skills_test.py` | all pass (53 today + the new ones), exit 0, runs in <1s |
| Lint | `uvx ruff check skills_test.py` | `All checks passed!`, exit 0 |
| Lint tests too | `uvx ruff check test_skills_test.py` | `All checks passed!`, exit 0 |

Run all commands with the working directory at `/Users/copyjosh/Code/skills-test`.
Line-length limit is 100 columns (configured for ruff) — keep every new line < 100.

## Scope

**In scope** (the only files you should modify):
- `/Users/copyjosh/Code/skills-test/skills_test.py`
- `/Users/copyjosh/Code/skills-test/test_skills_test.py` (add tests)

**Out of scope** (do NOT touch):
- `DiffEstimate`'s field list (`:1069-1081`) — it is the shared currency every
  renderer and the badge depend on; changing it ripples everywhere.
- The emitted JSON shapes: `summary_dict`'s top-level keys and the per-metric
  11-key field dict; `_chart_data`'s `{"point","lo","hi","p","q"}` blob keys. Keep
  both exactly as they are today (consumers and existing tests assert them).
- The embedded ES5 JS (`_HTML_SCRIPT`) and CSS (`_HTML_STYLE`) — no JS/CSS change
  is needed; only the Python that feeds the blob changes.
- `plans/001`–`plans/009`, `plans/README.md` (the reviewer owns the index).

## Git workflow

This repo is NOT git-initialized. Do not branch, commit, or push. Edit the two
in-scope files in place and verify with the commands above.

## Steps

Order: add the single source of truth (Step 1), repoint each renderer onto it
(Steps 2–4), then delete the now-dead helper (Step 5), then lock it with a test
(Step 6). The module stays runnable after every step.

### Step 1: Add `compute_estimates` — the single seeded pass

Add a new function near `estimate_diff` / `_pair_estimates` (top of the report
section, e.g. just above `build_report` at `:1234`, or right after
`estimate_diff`). It runs `estimate_diff` exactly ONCE per `(pair, mode, metric)`
with ONE `random.Random(seed)`, applies BH to each pair's ITT secondary metrics,
and returns `DiffEstimate` objects (the richest form — JSON callers down-convert
to field dicts). Target shape:

```python
def compute_estimates(results: list[RunResult], cfg: ExperimentConfig,
                      metrics: list[str], seed: int) -> dict:
    """Single source of truth for every pairwise estimate. Runs estimate_diff
    EXACTLY once per (pair, mode, metric) against ONE seeded RNG so the markdown,
    JSON and HTML renderers (and the badge) all read identical CIs/p-values.
    Returns {pair_key: {"a", "b", "itt": {metric: DiffEstimate|None},
    "pp": {metric: DiffEstimate|None}}}. BH q-values are applied to each pair's
    ITT secondary metrics (the primary metric stays uncorrected)."""
    rng = random.Random(seed)
    out: dict = {}
    for a, b in experiment_pairs(cfg):
        itt = {m: estimate_diff(results, m, "itt", cfg, rng, a, b) for m in metrics}
        sec = [m for m in metrics if m != cfg.primary_metric and itt[m]]
        for m, q in zip(sec, benjamini_hochberg([itt[m].p_value for m in sec])):
            itt[m].q_value = q
        pp = {m: estimate_diff(results, m, "pp", cfg, rng, a, b) for m in metrics}
        out[pair_key(cfg, a, b)] = {
            "a": arm_label(cfg, a), "b": arm_label(cfg, b), "itt": itt, "pp": pp}
    return out
```

The canonical call order is: per pair, all ITT metrics (in `metrics` list order),
then all PP metrics. This is the order summary_dict effectively used; markdown and
HTML will now adopt it too.

**Verify**: `python3 test_skills_test.py` → still all pass (function is
added, not yet wired). `uvx ruff check skills_test.py` → clean.

### Step 2: Render markdown `build_report` from `compute_estimates`

In `build_report` (`:1234`), delete the local `rng = random.Random(seed)` at
`:1240` and instead compute the estimates once:

```python
    est = compute_estimates(results, cfg, metrics, seed)
```

(`metrics` is already built at `:1241`.) Then in the per-pair loop (`:1301-1322`)
replace every `estimate_diff(...)` call with a lookup into `est[key]`:

- primary: `est[pair_key(cfg, a, b)]["itt"][primary]`
- secondary ITT rows: iterate `metrics` minus `primary`, take
  `est[key]["itt"][m]`, skip `None`. The q-values are ALREADY applied by
  `compute_estimates`, so DELETE the in-loop `benjamini_hochberg(...)` zip that
  currently mutates `e.q_value` (`:1314-1315`). Just render the rows.
- per-protocol rows: iterate `metrics`, take `est[key]["pp"][m]`, skip `None`.

Keep the no-shared-tasks placeholder line for a `None` primary (`:1309-1310`)
exactly as-is. `_row` is unchanged.

**Verify**: `python3 test_skills_test.py` → all pass (incl.
`test_single_task_badge_warning`, `test_head_to_head_*`). `uvx ruff check
skills_test.py` → clean.

### Step 3: Render JSON `summary_dict` from `compute_estimates`

In `summary_dict` (`:1674`): delete the local `rng = random.Random(seed)`
(`:1681`) and the `for a, b in experiment_pairs(cfg): comparisons[...] =
_pair_estimates(...)` block (`:1684-1688`). Replace with:

```python
    est = compute_estimates(results, cfg, metrics, seed)
    comparisons = {key: {"a": p["a"], "b": p["b"],
                         "itt": {m: _estimate_fields(e) for m, e in p["itt"].items() if e},
                         "per_protocol": {m: _estimate_fields(e)
                                          for m, e in p["pp"].items() if e}}
                   for key, p in est.items()}
```

Add a tiny helper that produces the EXACT 11-key field dict that
`_pair_estimates` produced (so the JSON shape is byte-identical):

```python
def _estimate_fields(e: DiffEstimate) -> dict:
    """The portable per-metric field dict the summary JSON has always emitted."""
    return {k: getattr(e, k) for k in (
        "mean_on", "mean_off", "point", "ci_low", "ci_high",
        "p_value", "q_value", "n_on", "n_off", "n_tasks", "clustered")}
```

Everything else in `summary_dict` (the `itt`/`per_protocol` primary mirror,
`validity`, `arm_stats`, `schema_version: 2`) stays untouched — it reads from
`comparisons[pkey]`, whose shape is preserved (per-comparison keys
`a`, `b`, `itt`, `per_protocol`; per-metric the same 11 keys).

**Verify**: `python3 test_skills_test.py` → `test_manifest_and_summary_shape`,
`test_head_to_head_three_arm_summary`, `test_ci_exit_code_policies`,
`test_demo_results_earns_verified_offline` all pass.

### Step 4: Render HTML `build_html_report` from `compute_estimates`

In `build_html_report` (`:3077`): delete the local `rng = random.Random(seed)`
(`:3085`) and the `pair_itt` construction block + its BH zip (`:3091-3098`).
Replace with:

```python
    est = compute_estimates(results, cfg, metrics, seed)
    pair_itt = {key: p["itt"] for key, p in est.items()}
```

`pair_itt` is then `{pair_key: {metric: DiffEstimate|None}}` exactly as
`_chart_data` and the verdict code at `:3100-3104` already expect — no further
change there. `metrics` is already built at `:3086`.

**Verify**: `python3 test_skills_test.py` →
`test_build_html_report_renders_and_escapes` passes. `uvx ruff check
skills_test.py` → clean.

### Step 5: Remove the now-dead `_pair_estimates`

`_pair_estimates` (`:1654-1671`) has no remaining caller after Step 3. Delete it
(no orphaned partial work). Grep to confirm nothing references it.

**Verify**: `grep -n "_pair_estimates" skills_test.py test_skills_test.py`
→ no matches. `python3 test_skills_test.py` → all pass.

### Step 6: Add the regression test that proves equality

See the Test plan. Add the fixture + test to `test_skills_test.py`.

**Verify**: `python3 test_skills_test.py` → all pass, including the new
test; `uvx ruff check test_skills_test.py` → clean.

## Test plan

The existing fixtures (`_two_task_results`, `_three_arm_results` at
`test_skills_test.py:334` and `:359`) are too degenerate to expose this bug:
their per-arm metric values are CONSTANT within each arm, so every bootstrap
resample yields the identical difference and the CI is `[point, point]`
regardless of RNG order. The regression test therefore needs WITHIN-arm variance
on the primary pair so the bootstrap CI actually depends on the RNG stream.

**New fixture** (add to `test_skills_test.py`): a 3-arm config with noisy
per-run scores, so the PRIMARY pair (`skill-a_vs_skill-b`, which is pair #3 —
exactly where the old code diverged) has a non-degenerate, RNG-sensitive CI.
Model it on `_three_arm_results` but add jitter and populate every default metric
(`tests_pass`, `lint_pass`, `build_pass`, `diff_lines`, `cost_usd`):

```python
def _noisy_three_arm_results():
    """3-arm with WITHIN-arm variance so the bootstrap CI depends on the RNG
    order — the only way to expose the cross-renderer divergence this plan fixes."""
    jit = random.Random(123)
    res = []
    base = {Arm.SKILL_ON: 0.8, Arm.SKILL_B: 0.55, Arm.SKILL_OFF: 0.2}
    names = {Arm.SKILL_ON: "skill-a", Arm.SKILL_B: "skill-b", Arm.SKILL_OFF: None}
    for t in ("A", "B"):
        for arm, b in base.items():
            for i in range(4):
                v = b + jit.uniform(-0.15, 0.15)
                r = RunResult(task_id=t, arm=arm, run_index=i, worktree=Path("/wt"),
                              skill_activated=(True if names[arm] else None),
                              activation_reason="", agent_ok=True, completed=True,
                              cost_usd=round(0.05 + 0.01 * i, 3), arm_skill_name=names[arm],
                              scores={"tests_pass": v, "lint_pass": 0.5 + 0.5 * (i % 2),
                                      "build_pass": 1.0, "diff_lines": 30.0 + 10 * v,
                                      "cost_usd": round(0.05 + 0.01 * i, 3)})
                res.append(r)
    return res
```

**New test** — the headline regression (extract the HTML blob, compare to the
summary JSON, demand bit-identical numbers for the PRIMARY pair):

```python
def test_estimates_identical_across_summary_and_html():
    import json
    cfg = _cfg(skill_name="skill-a", skill_b_src=Path("/s/b"), skill_b_name="skill-b")
    res = _noisy_three_arm_results()
    man = h.experiment_manifest(cfg, timestamp=1.0, offline=True)
    seed = 7
    s = h.summary_dict(res, cfg, man, seed=seed)
    doc = h.build_html_report(res, h.Preflight(), cfg, man, seed=seed)
    blob = json.loads(doc.split("window.SKILLS_TEST=", 1)[1].split(";\n", 1)[0])

    pkey = s["primary_pair"]                      # skill-a_vs_skill-b  == pair #3
    chart = next(c for c in blob["comparisons"] if c["key"] == pkey)
    # Primary metric CI must match BIT-for-BIT (summary uses ci_low/ci_high,
    # the HTML blob uses lo/hi for the same numbers):
    summ_p = s["comparisons"][pkey]["itt"]["tests_pass"]
    assert summ_p["ci_low"] == chart["itt"]["tests_pass"]["lo"]
    assert summ_p["ci_high"] == chart["itt"]["tests_pass"]["hi"]
    # A BH-corrected secondary metric's q-value must also match:
    summ_s = s["comparisons"][pkey]["itt"]["diff_lines"]
    assert summ_s["q"] == chart["itt"]["diff_lines"]["q"] \
        if False else summ_s["q_value"] == chart["itt"]["diff_lines"]["q"]
```

(Keep the assertion simple — `summ_s["q_value"] == chart["itt"]["diff_lines"]["q"]`.
Drop the dead `if False` ternary; it is shown only to flag that summary uses the
key `q_value` while the HTML blob uses `q`.)

Also add a markdown-vs-summary check (cheap, no blob parsing) — confirm the
markdown report's primary-pair CI text contains the same `ci_low`/`ci_high` the
summary reports, OR simply assert `compute_estimates` is order-stable:

```python
def test_compute_estimates_single_pass_is_order_stable():
    cfg = _cfg(skill_name="skill-a", skill_b_src=Path("/s/b"), skill_b_name="skill-b")
    res = _noisy_three_arm_results()
    metrics = [s.name for s in h.default_scorers(cfg)] + ["cost_usd"]
    a = h.compute_estimates(res, cfg, metrics, seed=7)
    b = h.compute_estimates(res, cfg, metrics, seed=7)
    pkey = "skill-a_vs_skill-b"
    assert a[pkey]["itt"]["tests_pass"].ci_low == b[pkey]["itt"]["tests_pass"].ci_low
    assert a[pkey]["itt"]["tests_pass"].ci_high == b[pkey]["itt"]["tests_pass"].ci_high
```

- Use the existing custom runner's convention: every `test_*` top-level function
  is auto-discovered (see how the file's other `test_*` functions are written —
  plain `assert`, no pytest fixtures).
- Run: `python3 test_skills_test.py` → all pass, including the 2 new tests.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `python3 test_skills_test.py` exits 0; all prior tests plus the new
      `test_estimates_identical_across_summary_and_html` and
      `test_compute_estimates_single_pass_is_order_stable` pass.
- [ ] `uvx ruff check skills_test.py` prints `All checks passed!` (exit 0).
- [ ] `uvx ruff check test_skills_test.py` prints `All checks passed!`.
- [ ] `grep -n "_pair_estimates" skills_test.py test_skills_test.py`
      returns no matches (dead helper removed).
- [ ] `grep -c "random.Random(seed)" skills_test.py` is lower than before:
      `build_report`, `summary_dict`, and `build_html_report` no longer each
      construct their own RNG; the only `random.Random(seed)` for pairwise
      estimates lives inside `compute_estimates`. (Other unrelated `random.Random`
      uses, e.g. the judge, may remain.)
- [ ] `DiffEstimate` field list at `:1069-1081` is unchanged.
- [ ] `summary_dict` still emits `schema_version: 2`, top-level `itt`,
      `per_protocol`, `validity`, `comparisons`, `primary_pair`, and per-metric
      the same 11 keys (`mean_on`…`clustered` + `q_value`).
- [ ] No dependency added — `grep -nE "^import |^from " skills_test.py`
      shows only stdlib modules (no numpy/pandas/jinja/pytest).
- [ ] No file outside the in-scope list modified; `plans/README.md` NOT edited.

## STOP conditions

Stop and report (do not improvise) if:

- Any "Current state" excerpt does not match the live code (the module drifted
  since this plan was written — line numbers may move, but the code must match).
- Removing a per-renderer `estimate_diff` call would drop a field that a consumer
  reads. Concretely: `primary_verdict`/`badge_verdict`/`ci_exit_code` read
  `summary["itt"][metric]` keys `ci_low`, `ci_high`, `point`, `n_tasks`,
  `clustered`; `_chart_data` reads `DiffEstimate.point/ci_low/ci_high/p_value/
  q_value`. If your refactor cannot preserve every one of these, STOP.
- An existing test that asserts a `point` value or a verdict `label`
  (`test_head_to_head_three_arm_summary`, `test_demo_results_earns_verified_offline`,
  `test_ci_exit_code_policies`) FAILS — `point` is RNG-independent and verdict
  labels should not flip, so a failure means the refactor changed estimand
  semantics, not just RNG order. Investigate before continuing.
- The new equality test still fails AFTER the refactor — that means a renderer is
  still calling `estimate_diff` outside `compute_estimates`, or the call order
  inside `compute_estimates` differs from what a renderer consumes. Re-check that
  ALL three renderers read from the single structure and that none makes its own
  RNG.
- A verification command fails twice after a reasonable fix attempt.

## Maintenance notes

For whoever owns this code next:

- `compute_estimates` is now the ONLY place pairwise CIs/p-values are produced.
  Any new renderer or export MUST read from it (or from `summary_dict`'s output)
  rather than calling `estimate_diff` again with a fresh RNG — re-introducing a
  second RNG pass would re-create exactly the divergence this plan removed.
- The canonical RNG-consumption order is "per pair: all ITT metrics, then all PP
  metrics, in `metrics` list order." If you reorder metrics or add a mode, the CI
  numbers will change (legitimately) but will STAY consistent across renderers
  because they all draw from this one pass. Do not special-case the primary
  metric out of the loop (the old markdown code did, which was part of the bug).
- Reviewer focus: confirm `summary_dict`'s JSON is byte-shape-identical to before
  (the `_estimate_fields` 11-key dict and the `per_protocol` key name), and that
  the HTML blob still uses `lo`/`hi`/`p`/`q`. The portable schema in
  `results.schema.json` and the badge both depend on these.
- Deferred out of scope: this plan does not memoize across the three call sites in
  `_run_and_outputs` (they still each call `compute_estimates` once with seed 0).
  That is 3 cheap passes instead of the old 7-ish; collapsing to a single shared
  call object is a further perf win but a larger refactor of the CLI plumbing —
  leave it unless profiling shows it matters.
