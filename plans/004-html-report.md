# Plan 004: Self-contained HTML report — the shareable artifact

> **Executor instructions**: Follow step by step; verify each step; honor STOP
> conditions; update this plan's row in `plans/README.md` when done.
>
> **Drift check**: repo not git-tracked. Confirm `RunResult`/`to_dict`,
> `build_report`, `DiffEstimate`, and the judge structs match the excerpts before
> editing. Mismatch → STOP.

## Status

- **Priority**: P1
- **Effort**: L
- **Risk**: MED (largest surface; pure-additive though)
- **Depends on**: 002 (manifest/summary)
- **Category**: direction / reviewability
- **Planned at**: 2026-06-25 (repo not git-initialized)

## Why this matters

The only output today is a Markdown table on stdout (`build_report`). It tells you
the skill helped but not **why**, so a skeptic can't audit it and you can't explain
it. This plan emits ONE self-contained `.html` file (no external assets) that
renders: the stats tables, a **side-by-side ON-vs-OFF diff drill-down** per task
(the captured `RunResult.diff` is already on disk in `results.jsonl`, consumed only
by the judge today), a **per-run drill-down** (cost, turns, verbatim
`activation_reason`), **distribution dots** (inline SVG, colored by task — makes
the cluster bootstrap legible), and the **judge panel** (per-pair reasons +
position-flips). It reads straight from `results.jsonl`, so it works post-hoc. This
is the object people drag into a PR or DM — the thing that converts skeptics.

## Current state

- `class RunResult` (`skills_test.py:145`) with `to_dict()` (around line 181)
  serializing every field incl. `diff`, `scores`, `cost_usd`, `num_turns`,
  `activation_reason`, `skill_activated`, `itt_valid` (property). **There is no
  `from_dict`.**
- `run_experiment` (`:725`) writes `results_dir/results.jsonl`, one
  `json.dumps(res.to_dict())` per line.
- `RunResult.diff` (set in `execute_run` ~line 690) holds the work product
  (already `.claude`-excluded for blinding by plan… it is excluded via
  `git diff HEAD -- . ':(exclude).claude'`).
- `build_report(results, pf, cfg, scorers, seed, manifest)` (`:928`, manifest param
  added by plan 002) — returns Markdown; computes per-metric `estimate_diff`.
- Judge: `run_qualitative_judge` → `list[JudgeComparison]` (struct at
  `skills_test.py:1037`: `task_id, pair_id, ordering, winner_arm, reason`),
  `aggregate_judge` (`:1195`), `build_judge_report` (`:1215`).
- Convention: stdlib only — use `html.escape`, string templates, inline `<style>`
  and inline `<svg>`. No JS framework; a tiny `<details>`/`<summary>` for collapse
  needs no JS.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Compile | `python3 -m py_compile skills_test.py test_skills_test.py` | exit 0 |
| Tests | `python3 test_skills_test.py` | `N passed` |
| Line length | plan-001 Step-5 snippet | `OK` |

## Scope

**In scope:** `skills_test.py` (add `RunResult.from_dict`, `load_results`,
`_diff_to_html`, `_dist_svg`, `build_html_report`, and a `report` CLI subcommand);
`test_skills_test.py` (from_dict round-trip + html-render tests).

**Out of scope:** the statistics, the judge logic, the Markdown report body (reuse
`estimate_diff`/`aggregate_judge`; do not duplicate or alter them).

## Steps

### Step 1: `RunResult.from_dict` + `load_results`

Add a classmethod mirroring `to_dict` (skip derived/property keys
`contaminated/itt_valid/pp_valid` — they're recomputed):
```python
    @classmethod
    def from_dict(cls, d: dict) -> "RunResult":
        return cls(
            task_id=d["task_id"], arm=Arm(d["arm"]), run_index=d["run_index"],
            worktree=Path(d["worktree"]), skill_activated=d["skill_activated"],
            activation_reason=d.get("activation_reason", ""), agent_ok=d["agent_ok"],
            completed=d.get("completed", False), timed_out=d.get("timed_out", False),
            cost_usd=d.get("cost_usd"), num_turns=d.get("num_turns"),
            hit_turn_limit=d.get("hit_turn_limit", False),
            wall_seconds=d.get("wall_seconds"), scores=dict(d.get("scores") or {}),
            error=d.get("error"), diff=d.get("diff"))
```
```python
def load_results(path: Path) -> list[RunResult]:
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if line:
            out.append(RunResult.from_dict(json.loads(line)))
    return out
```
**Verify** (test Step 5): `RunResult.from_dict(r.to_dict())` reconstructs a run with
equal `task_id/arm/scores/diff` and the same `itt_valid`.

### Step 2: Diff → HTML (escaped, +/- colored)

```python
def _diff_to_html(diff: str | None) -> str:
    import html
    if not diff:
        return '<p class="empty">(no diff)</p>'
    rows = []
    for line in diff.splitlines():
        cls = "ctx"
        if line.startswith("+") and not line.startswith("+++"):
            cls = "add"
        elif line.startswith("-") and not line.startswith("---"):
            cls = "del"
        elif line.startswith("@@"):
            cls = "hunk"
        rows.append(f'<div class="{cls}">{html.escape(line) or "&nbsp;"}</div>')
    return f'<pre class="diff">{"".join(rows)}</pre>'
```

### Step 3: Distribution dots (inline SVG, colored by task)

For a metric, plot each valid run's value as a dot, x=jittered by arm, color by
task — so the reader SEES the spread the cluster bootstrap accounts for.
```python
def _dist_svg(results: list[RunResult], metric: str) -> str:
    pts = [(r.task_id, r.arm, r.scores[metric]) for r in results
           if r.itt_valid and metric in r.scores]
    if not pts:
        return ""
    tasks = sorted({t for t, _, _ in pts})
    palette = ["#4c78a8", "#f58518", "#54a24b", "#e45756", "#72b7b2", "#b279a2"]
    colors = {t: palette[i % len(palette)] for i, t in enumerate(tasks)}
    vals = [v for _, _, v in pts]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1.0
    W, H = 320, 80
    def x(arm):  # ON left, OFF right
        return 70 if arm is Arm.SKILL_ON else 250
    dots = []
    for i, (t, arm, v) in enumerate(pts):
        jx = x(arm) + ((i * 37) % 40) - 20
        cy = H - 10 - (v - lo) / span * (H - 20)
        dots.append(f'<circle cx="{jx}" cy="{cy:.1f}" r="3" fill="{colors[t]}" '
                    f'opacity="0.75"><title>{t} {arm.value} {v}</title></circle>')
    return (f'<svg width="{W}" height="{H}" class="dist">'
            f'<text x="70" y="14" font-size="10">skill_on</text>'
            f'<text x="250" y="14" font-size="10">skill_off</text>'
            f'{"".join(dots)}</svg>')
```

### Step 4: `build_html_report` + `report` subcommand

Add `build_html_report(results, pf, cfg, manifest, comparisons=None, seed=0) -> str`
that returns one HTML document:
- `<head>` with an inline `<style>` (`.add{color:#22863a;background:#e6ffed}`,
  `.del{color:#b31d28;background:#ffeef0}`, `.diff{font:12px monospace;...}`, table CSS).
- A header with the manifest (from plan 002) and the primary verdict.
- The stats tables: iterate the SAME `estimate_diff(results, m, "itt"/"pp", cfg, rng)`
  values `build_report` uses; render as HTML `<table>` (don't re-implement the math).
- Per task: a `<details>` with two columns — ON runs' `_diff_to_html(r.diff)` and
  OFF runs' — plus the `_dist_svg` for the primary metric.
- A per-run table: arm, index, `itt_valid`, `skill_activated`, `cost_usd`,
  `num_turns`, and verbatim `activation_reason` (the audit trail).
- If `comparisons` given: a judge panel listing each pair's two orderings,
  `winner_arm`, `reason`, and a "position-flip" flag when the two orderings disagree
  (reuse `aggregate_judge` for the win-rate line).

CLI:
```python
prep = sub.add_parser("report", help="render an HTML report from results.jsonl")
prep.add_argument("-c", "--config", type=Path, default=Path("skillab.toml"))
prep.add_argument("--jsonl", type=Path, default=None, help="defaults to <results_dir>/results.jsonl")
prep.add_argument("-o", "--out", type=Path, default=Path("skills-test-report.html"))
```
handler: load cfg (plan 001 `load_config`), `results = load_results(jsonl or cfg.results_dir/'results.jsonl')`,
build manifest (plan 002), write `build_html_report(...)` to `--out`, print the path.
This spends **zero** `claude`/`git` — pure render. (Judge panel only if a
`comparisons.json` exists; otherwise omit.)

**Verify**: with a `results.jsonl`, `python3 skills_test.py report` writes an
`.html`; `grep -c "<table" skills-test-report.html` ≥ 1 and `grep -c "class=\"diff\"" ...` ≥ 1.

### Step 5: Tests

```python
def test_runresult_from_dict_round_trips():
    r = _rr(Arm.SKILL_ON, True, scores={"tests_pass": 1.0}); r.diff = "diff --git a b\n+x"
    r2 = h.RunResult.from_dict(r.to_dict())
    assert r2.task_id == r.task_id and r2.arm is r.arm
    assert r2.scores == r.scores and r2.diff == r.diff and r2.itt_valid == r.itt_valid

def test_build_html_report_renders():
    cfg = _cfg()
    res = []
    for t in ("A", "B"):
        for arm, v in ((Arm.SKILL_ON, 1.0), (Arm.SKILL_OFF, 0.0)):
            rr = _rr(arm, arm is Arm.SKILL_ON, scores={"tests_pass": v}); rr.task_id = t
            rr.diff = f"diff for {t} {arm.value}\n+line"
            res.append(rr)
    man = h.experiment_manifest(cfg, timestamp=1.0)
    html_doc = h.build_html_report(res, h.Preflight(), cfg, man)
    assert html_doc.lstrip().startswith("<") and "</html>" in html_doc
    assert "diff for A" in html_doc and "<table" in html_doc
```

**Verify**: `python3 test_skills_test.py` → `N passed`; line-length `OK`.

## Test plan

- `test_runresult_from_dict_round_trips` — JSONL persistence is lossless for the
  fields the report needs; `itt_valid` recomputes identically.
- `test_build_html_report_renders` — produces a complete HTML doc containing the
  tables and the escaped diff text. (Don't assert exact markup — assert key markers.)
- Manually open the generated `.html` once to eyeball the diff columns + dots.

## Done criteria

- [ ] `python3 -m py_compile …` exits 0
- [ ] `RunResult.from_dict(r.to_dict())` round-trips (test passes)
- [ ] `build_html_report` returns a self-contained doc (`<html>…</html>`, inline CSS, no external `src=`/`href=` to assets)
- [ ] `report` subcommand renders from `results.jsonl` with no `claude`/`git` calls
- [ ] diff columns + distribution SVG + per-run audit table present
- [ ] new tests pass; line-length `OK`
- [ ] `plans/README.md` row for 004 updated

## STOP conditions

- `to_dict` keys differ from the `from_dict` mapping (file drifted) — STOP; a wrong
  mapping silently corrupts every loaded run.
- `RunResult.diff` is not present in the JSONL (someone ran with `capture_diffs=False`)
  — the diff columns will be empty; that's expected, not a bug, but note it in the
  report ("diffs not captured") rather than failing.
- The HTML would need a JS framework or external asset to work — STOP; the artifact
  must be a single offline file.

## Maintenance notes

- `from_dict`/`to_dict` must stay symmetric — add a guard test if new fields appear.
- Keep escaping rigorous: diffs are agent-authored text and go straight into HTML;
  every diff/reason/path must pass through `html.escape`. Reviewer: grep the
  rendering for any interpolation that bypasses `html.escape`.
- Plan 005 (`--demo`) renders via this exact path over a bundled `results.jsonl`.
