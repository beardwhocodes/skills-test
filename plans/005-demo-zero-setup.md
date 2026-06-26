# Plan 005: `--demo` — zero-setup, zero-cost proof in one command (flagship)

> **Executor instructions**: Follow step by step; verify each step; honor STOP
> conditions; update this plan's row in `plans/README.md` when done.
>
> **Drift check**: repo not git-tracked. Confirm plans 001, 003, 004 have landed
> (`main()` with subparsers, `render_badge_svg`, `build_html_report`,
> `load_results`). If any is missing, STOP — this plan composes them.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: LOW
- **Depends on**: 001 (CLI), 003 (badge), 004 (HTML report + `load_results`)
- **Category**: direction / virality
- **Planned at**: 2026-06-25 (repo not git-initialized)

## Why this matters

The flagship adoption move: `uvx skill-ab-harness demo` produces a real HTML
report + badge **in seconds, with zero config, zero auth, zero cost** — by
rendering a *bundled, pre-recorded* `results.jsonl` instead of spending money on
`claude`. That is the tweetable one-liner and the thing a newcomer runs first to
understand what the tool even outputs. A `--live` switch runs the real pipeline on
a generated fixture for those who want to see it actually drive `claude`. Three
independent ideators surfaced `--demo` as the single highest-leverage item.

## Current state (after deps land)

- `main()` (plan 001) — argparse with subcommands; `load_config`.
- `build_html_report(results, pf, cfg, manifest, comparisons=None)` and
  `load_results(path)` (plan 004).
- `render_badge_svg` / `badge_verdict` / `summary_dict` / `experiment_manifest`
  (plans 002–003).
- `run_experiment` / `run_qualitative_judge` exist for the `--live` path.
- No `fixtures/` directory exists yet. Convention: stdlib only.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Compile | `python3 -m py_compile skill_ab_harness.py test_skill_ab_harness.py` | exit 0 |
| Tests | `python3 test_skill_ab_harness.py` | `N passed` |
| Demo (offline) | `python3 skill_ab_harness.py demo --out /tmp/demo` | writes report.html + badge.svg, exit 0, NO network |
| Line length | plan-001 Step-5 snippet | `OK` |

## Scope

**In scope:** `skill_ab_harness.py` (add `cmd_demo` + a `demo` subparser);
`fixtures/demo_results.jsonl` (new, pre-recorded), `fixtures/demo_skillab.toml`,
`fixtures/demo_skill/SKILL.md` (new bundled sample); `test_skill_ab_harness.py`
(demo-renders test); `pyproject.toml` (ensure fixtures ship — see Step 4).

**Out of scope:** changing `run_experiment`, the stats, or the report internals.
The demo is a thin composition over existing functions + static fixtures.

## Steps

### Step 1: Author the bundled fixture (a believable, honest sample)

Create `fixtures/demo_skill/SKILL.md` — a short, realistic skill (e.g. a
"write-tests-first" skill) so the diffs in the demo look real.

Create `fixtures/demo_results.jsonl` — ~24 lines (2 tasks × 2 arms × k=6) of
`RunResult.to_dict()` records, hand-authored to tell a clear but HONEST story: ON
arm has a higher `tests_pass` rate and small realistic `diff` blobs; OFF arm lower;
include one `skill_activated=false` ON run (ITT keeps it) and zero contaminated OFF
runs so the badge can legitimately read "verified". Each line must be valid for
`RunResult.from_dict` (keys: `task_id, arm, run_index, worktree, skill_activated,
activation_reason, agent_ok, completed, cost_usd, num_turns, scores, diff`). Keep
`diff` values short, real-looking unified diffs.

Create `fixtures/demo_skillab.toml` — a config whose `skill_name` matches the
fixture and `results_dir` is a temp dir (overridden at runtime anyway).

**Verify**:
```bash
python3 -c "
import skill_ab_harness as h
rs=h.load_results(__import__('pathlib').Path('fixtures/demo_results.jsonl'))
assert len(rs)>=12 and any(r.itt_valid for r in rs); print('fixture loads', len(rs))"
```

### Step 2: `cmd_demo`

```python
def cmd_demo(out_dir: Path, live: bool = False) -> None:
    """Offline (default): render the bundled fixture into an HTML report + badge
    with ZERO claude/git/network/cost. --live: actually run the real pipeline on a
    generated throwaway repo (costs claude $)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    here = Path(__file__).resolve().parent
    if live:
        raise SystemExit("demo --live: generate a temp git repo + skill and call "
                         "run_experiment (see plan 005 maintenance notes); not in the "
                         "offline path")  # implement in a follow-up; offline ships first
    results = load_results(here / "fixtures" / "demo_results.jsonl")
    cfg = ExperimentConfig(repo_path=here, base_ref="HEAD",
                           skill_src=here / "fixtures" / "demo_skill",
                           skill_name="demo-skill", results_dir=out_dir, k=6)
    manifest = experiment_manifest(cfg, seed=0, timestamp=0.0)
    # HTML report
    html_path = out_dir / "report.html"
    html_path.write_text(build_html_report(results, Preflight(), cfg, manifest))
    # Badge from the summary
    summary = summary_dict(results, cfg, manifest)
    est = summary["itt"].get(cfg.primary_metric)
    svg_path = out_dir / "badge.svg"
    if est:
        v = badge_verdict(est, 1, est["n_tasks"], est["clustered"],
                          summary["validity"]["off_contaminated"])
        svg_path.write_text(render_badge_svg(cfg.primary_metric, v))
    print(f"demo report: {html_path}\ndemo badge:  {svg_path}\n"
          f"open the HTML to see the on/off diff drill-down. No money spent.")
```
Add the subparser in `main()`:
```python
pd = sub.add_parser("demo", help="render a bundled example report+badge (offline, free)")
pd.add_argument("-o", "--out", type=Path, default=Path("skill-ab-demo"))
pd.add_argument("--live", action="store_true", help="actually run claude on a generated fixture (costs $)")
```
handler: `cmd_demo(args.out, args.live); return 0`.

**Verify**:
```bash
python3 skill_ab_harness.py demo --out /tmp/demo
test -s /tmp/demo/report.html && test -s /tmp/demo/badge.svg && echo "demo ok"
grep -q "verified\|inconclusive" /tmp/demo/badge.svg && echo "badge rendered"
```
Confirm it makes NO network/`claude`/`git` calls (offline path only reads the
fixture file).

### Step 3: Test

```python
def test_demo_renders_offline(tmp_path=None):
    import tempfile, pathlib
    out = pathlib.Path(tempfile.mkdtemp())
    h.cmd_demo(out, live=False)
    assert (out / "report.html").read_text().count("<table") >= 1
    assert (out / "badge.svg").exists()
```

**Verify**: `python3 test_skill_ab_harness.py` → `N passed`; line-length `OK`.

### Step 4: Make fixtures ship with the package

In `pyproject.toml`, ensure the `fixtures/` data is included for `uvx`/`pip`
installs. With a single-module project, add:
```toml
[tool.setuptools]
py-modules = ["skill_ab_harness"]

[tool.setuptools.package-data]
"*" = ["fixtures/**"]
```
(If the project already declares a build backend, match it; otherwise add
`[build-system]` with setuptools. STOP and report if the existing build config
conflicts — don't guess a second backend.)

**Verify**: `python3 -c "from pathlib import Path; assert (Path('fixtures/demo_results.jsonl')).exists()"`.

## Test plan

- `test_demo_renders_offline` — the offline demo writes a non-trivial HTML report
  and a badge, with no external calls. This is the path users hit first; it must
  never depend on `claude`/auth/network.
- Manually run `python3 skill_ab_harness.py demo` and open the HTML once.

## Done criteria

- [ ] `python3 -m py_compile …` exits 0
- [ ] `python3 skill_ab_harness.py demo --out /tmp/demo` exits 0 and writes a non-empty `report.html` + `badge.svg`
- [ ] the offline demo makes NO `claude`/network calls (verify by reading `cmd_demo`: only file reads + render functions)
- [ ] badge reads "verified" (the fixture is authored to legitimately earn it: CI excludes 0, ≥2 tasks, 0 contaminated)
- [ ] `fixtures/` ships per `pyproject.toml`
- [ ] new test passes; line-length `OK`
- [ ] `plans/README.md` row for 005 updated

## STOP conditions

- Any dependency function (`build_html_report`, `render_badge_svg`,
  `load_results`, `summary_dict`) is missing — plans 002–004 not landed; STOP.
- The fixture can't legitimately earn a "verified" badge without faking the stats
  — STOP and reconsider the fixture numbers; do NOT special-case the badge logic to
  force green (that would destroy the credibility the badge exists for).
- The existing build backend conflicts with Step 4 — STOP and report.

## Maintenance notes

- Keep the fixture HONEST: if `summary_dict`/`badge_verdict` logic changes, re-verify
  the demo still earns the badge it claims; never patch the badge to force a color.
- The `--live` path is intentionally deferred to a follow-up: it must create a throwaway
  git repo (`git init`, a trivial failing-test fixture), point `cfg.repo_path` at it,
  run `run_experiment`, then render — gated behind `--live` because it spends money.
  Implement it only when someone asks; the offline demo is the viral artifact.
- Reviewer: confirm `demo` cannot be made to phone home or spend money in its
  default (offline) form.
