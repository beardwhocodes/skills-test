# Plan 003: Verdict badge — the brag object

> **Executor instructions**: Follow step by step; verify each step; honor STOP
> conditions; update this plan's row in `plans/README.md` when done.
>
> **Drift check**: repo not git-tracked. Confirm `DiffEstimate`, `summary_dict`
> (from plan 002), and `_row`'s significance rule still match the excerpts. Mismatch → STOP.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: 002 (reads `summary.json` / `DiffEstimate` fields)
- **Category**: direction / virality
- **Planned at**: 2026-06-25 (repo not git-initialized)

## Why this matters

Every skill author wants a one-line, defensible claim on their repo. A badge with
a real confidence interval — `skill verified: +18% tests_pass, 95% CI [+6%,+30%]`
— is the antidote to "my skill helped (vibes)" and is inherently viral: it sits in
READMEs, links back to the harness, and pressures other authors to produce one.
All inputs are already computed (the primary `DiffEstimate`); this is just an
emitter. Crucially the badge is **self-policing**: grey/"inconclusive" unless the
result is actually trustworthy, so it can't be used to oversell.

## Current state

- `class DiffEstimate` (`skill_ab_harness.py:765`): has `point`, `ci_low`,
  `ci_high`, `n_tasks`, `clustered`, `q_value`, etc.
- Significance convention (in `_row`, `skill_ab_harness.py:909`): a result is
  "significant" when the 95% CI excludes 0, i.e. `not (ci_low <= 0 <= ci_high)`.
  `†`/`‡` flag `clustered is False` / `n_tasks < 2` (untrustworthy).
- Plan 002 adds `summary_dict(...)["itt"][metric]` carrying `point/ci_low/ci_high/
  n_tasks/clustered` and `["validity"]["off_contaminated"]`, and
  `cfg.primary_metric` (default `"tests_pass"`).
- Direction per metric: `default_scorers` sets `tests_pass` direction +1
  (bigger-better). `cost_usd`/`diff_lines` are −1 (smaller-better).
- Convention: stdlib only — build the SVG with string templating (no svgwrite).

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Compile | `python3 -m py_compile skill_ab_harness.py test_skill_ab_harness.py` | exit 0 |
| Tests | `python3 test_skill_ab_harness.py` | `N passed` |
| Line length | plan-001 Step-5 snippet | `OK` |

## Scope

**In scope:** `skill_ab_harness.py` (add `badge_verdict`, `render_badge_svg`,
`badge_endpoint_json`, `badge_markdown`, and a `badge` CLI subcommand);
`test_skill_ab_harness.py` (verdict-logic tests).

**Out of scope:** statistics, the significance rule itself (reuse it), the
Markdown/HTML reports.

## Steps

### Step 1: Verdict + color logic (the credibility gate)

```python
def badge_verdict(est_dict: dict, direction: int, n_tasks: int, clustered: bool,
                  contaminated: int) -> dict:
    """Map a primary-metric estimate to a badge. Self-policing: only claim a
    win/regression when the 95% CI excludes 0 AND the run is trustworthy
    (>=2 clustered tasks, no OFF-arm contamination). Otherwise 'inconclusive'."""
    lo, hi, point = est_dict["ci_low"], est_dict["ci_high"], est_dict["point"]
    trustworthy = clustered and n_tasks >= 2 and contaminated == 0
    excludes_zero = not (lo <= 0 <= hi)
    if not trustworthy or not excludes_zero:
        label, color = "inconclusive", "lightgrey"
    else:
        improved = (point > 0) if direction > 0 else (point < 0)
        label = "verified" if improved else "regressed"
        color = "brightgreen" if improved else "red"
    # express the delta in the metric's natural direction-of-good
    return {"label": label, "color": color, "point": point,
            "ci_low": lo, "ci_high": hi, "n_tasks": n_tasks}
```

**Verify** (Step 5 covers it): a CI of `[+0.06,+0.30]` on a +1 metric, 2 clustered
tasks, 0 contaminated → `verified`/`brightgreen`; CI `[-0.1,+0.2]` → `inconclusive`.

### Step 2: Render the SVG (stdlib string template)

```python
def render_badge_svg(metric: str, verdict: dict) -> str:
    """A flat shields-style SVG. No external assets/deps."""
    import html
    pct = f"{verdict['point']*100:+.0f}%"
    right = f"{verdict['label']} {metric} {pct}" if verdict["label"] != "inconclusive" \
        else f"{metric}: inconclusive"
    right = html.escape(right)
    color = {"brightgreen": "#4c1", "red": "#e05d44", "lightgrey": "#9f9f9f"}[verdict["color"]]
    lw, rw = 70, max(120, 8 * len(right))
    w = lw + rw
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="20" '
        f'role="img" aria-label="skills-test: {right}">'
        f'<rect width="{lw}" height="20" fill="#555"/>'
        f'<rect x="{lw}" width="{rw}" height="20" fill="{color}"/>'
        f'<g fill="#fff" font-family="Verdana,Geneva,sans-serif" font-size="11">'
        f'<text x="6" y="14">skills-test</text>'
        f'<text x="{lw+6}" y="14">{right}</text></g></svg>')

def badge_endpoint_json(metric: str, verdict: dict) -> dict:
    """shields.io 'endpoint' schema (host the JSON, point shields at it)."""
    pct = f"{verdict['point']*100:+.0f}%"
    msg = f"{verdict['label']} {pct}" if verdict["label"] != "inconclusive" else "inconclusive"
    return {"schemaVersion": 1, "label": f"skills-test: {metric}",
            "message": msg, "color": verdict["color"]}

def badge_markdown(metric: str, verdict: dict, svg_rel_path: str) -> str:
    ci = f"[{verdict['ci_low']*100:+.0f}%, {verdict['ci_high']*100:+.0f}%]"
    return (f"![skills-test {metric}]({svg_rel_path}) "
            f"<!-- {verdict['label']} {metric} {verdict['point']*100:+.0f}% 95% CI {ci}, "
            f"n_tasks={verdict['n_tasks']} -->")
```

### Step 3: Wire the `badge` subcommand (reads plan-002 `summary.json`)

In `main()` (added by plan 001), add a subparser:
```python
pb = sub.add_parser("badge", help="emit an SVG + markdown badge from summary.json")
pb.add_argument("-s", "--summary", type=Path, default=Path("summary.json"))
pb.add_argument("-o", "--out", type=Path, default=Path("skill-ab-badge.svg"))
```
and a handler:
```python
    if args.cmd == "badge":
        summary = json.loads(args.summary.read_text())
        metric = summary["primary_metric"]
        est = summary["itt"].get(metric)
        if not est:
            raise SystemExit(f"no ITT estimate for primary metric '{metric}' in {args.summary}")
        direction = 1  # tests_pass is bigger-better; map from scorers if customized
        verdict = badge_verdict(est, direction, est["n_tasks"], est["clustered"],
                                summary["validity"]["off_contaminated"])
        args.out.write_text(render_badge_svg(metric, verdict))
        print(badge_markdown(metric, verdict, str(args.out)))
        return 0
```
(If plan 001 isn't landed, expose `badge` by calling these functions in a REPL;
the functions are the deliverable, the subcommand is the convenience.)

**Verify**: with a `summary.json` from plan 002, `python3 skill_ab_harness.py
badge` writes an `.svg` and prints a Markdown line; `grep -c "<svg" skill-ab-badge.svg` → ≥1.

### Step 4: Tests

```python
def test_badge_verdict_self_polices():
    base = {"point": 0.18, "ci_low": 0.06, "ci_high": 0.30}
    v = h.badge_verdict(base, direction=1, n_tasks=3, clustered=True, contaminated=0)
    assert v["label"] == "verified" and v["color"] == "brightgreen"
    # CI straddles 0 -> inconclusive
    v2 = h.badge_verdict({"point": 0.1, "ci_low": -0.05, "ci_high": 0.25},
                         1, 3, True, 0)
    assert v2["label"] == "inconclusive" and v2["color"] == "lightgrey"
    # trustworthy gate: contamination or <2 tasks or unclustered -> inconclusive
    assert h.badge_verdict(base, 1, 1, True, 0)["label"] == "inconclusive"
    assert h.badge_verdict(base, 1, 3, False, 0)["label"] == "inconclusive"
    assert h.badge_verdict(base, 1, 3, True, 2)["label"] == "inconclusive"
    # a clear regression on a bigger-better metric
    reg = {"point": -0.2, "ci_low": -0.3, "ci_high": -0.05}
    assert h.badge_verdict(reg, 1, 3, True, 0)["label"] == "regressed"

def test_render_badge_svg_is_svg():
    v = h.badge_verdict({"point": 0.18, "ci_low": 0.06, "ci_high": 0.30}, 1, 3, True, 0)
    svg = h.render_badge_svg("tests_pass", v)
    assert svg.startswith("<svg") and "tests_pass" in svg
```

**Verify**: `python3 test_skill_ab_harness.py` → `N passed`; line-length `OK`.

## Test plan

- `test_badge_verdict_self_polices` — happy win, straddle→inconclusive, and each
  trustworthiness gate (n_tasks<2, unclustered, contaminated) → inconclusive, plus
  a regression. This is the credibility-critical logic; cover every branch.
- `test_render_badge_svg_is_svg` — output is well-formed-ish SVG containing the metric.

## Done criteria

- [ ] `python3 -m py_compile …` exits 0
- [ ] `badge_verdict` returns `inconclusive` unless CI excludes 0 AND trustworthy
- [ ] `render_badge_svg` returns a `<svg …>` string
- [ ] `badge` subcommand writes an SVG and prints Markdown (if plan 001 landed)
- [ ] new tests pass; line-length `OK`
- [ ] `plans/README.md` row for 003 updated

## STOP conditions

- `summary.json` lacks `itt`/`validity` keys (plan 002 not landed or changed) — STOP.
- The significance convention in `_row` is no longer "CI excludes 0" — STOP and
  reconcile so the badge matches the report (they must never disagree).

## Maintenance notes

- The hardcoded `direction = 1` in the subcommand assumes the primary metric is
  bigger-better (true for `tests_pass`). If someone sets `primary_metric` to a
  smaller-better metric (cost/diff), thread the real direction from the scorer
  list into `summary.json` (plan 002) and read it here.
- Reviewer: the badge must NEVER show green on a contaminated/underpowered run —
  that's the whole point. Scrutinize the trustworthiness gate.
- A future `--ci` plan reuses `badge_verdict` for its pass/fail exit code.
