# Plan 020: Spike a static results gallery on the portable summary.json substrate

> **Executor instructions**: This is a DESIGN / SPIKE plan, not a build-everything
> plan. Your deliverables are (1) the decisions recorded in this file's "Design
> deliverable" section restated as your findings, (2) the open-questions list, and
> (3) ONE tiny prototype: a pure `build_gallery_html(...)` function plus a verdict
> helper, rendered from TWO generated `summary.json` fixtures in a test. Do NOT
> wire a full `gallery` CLI subcommand, do NOT add hosting, do NOT touch the
> existing report/badge code paths. Run every verification command and confirm the
> expected result before moving on. If anything in "STOP conditions" occurs, stop
> and report — do not improvise. When done, your row will be added to
> `plans/README.md` by the reviewer — do NOT edit that index yourself.
>
> **Drift check (run first)**: this repo is NOT git-initialized, so there is no SHA
> to diff against. Instead, compare the "Current state" excerpts below to the live
> code in `skill_ab_harness.py` before editing. On any mismatch, treat it as a STOP
> condition.

## Status

- **Priority**: P3
- **Effort**: L (spike)
- **Risk**: MED (strategic — see "Why this matters" and "Honest risk assessment")
- **Depends on**: none
- **Category**: direction
- **Planned at**: not git-initialized, 2026-06-25
- **Issue**: —

## Why this matters

The repo's thesis (`plans/README.md` → "The thesis") is a viral loop: an author
runs the harness → gets a badge + an HTML report showing *why* their skill helped
→ pastes the badge in their skill's README (which links back) → others see it and
run their own. The badge is a paste-able image; the report is a static file you DM
or attach to a PR. **The missing last mile is a place those badges can point to** —
a single page where many runs are listed side by side, so a skill author can say
"here is the gallery of A/B results" rather than linking one PR at a time.

`plans/README.md` deliberately did NOT build this ("the public gallery/leaderboard
hosting is intentionally NOT built … the portable `summary.json` +
`results.schema.json` are the substrate that makes one possible later"), and the
"Findings considered and rejected" list explicitly rejects **hosted SaaS / accounts
/ a results backend**. So the gallery, IF built, must be **static only**: no
backend, no accounts, no upload endpoint. This spike's job is to de-risk that
decision — define the API/surface, nail down the trust model for self-reported
numbers, decide the schema-migration and hosting story, and prove the rendering is
feasible with a tiny prototype — WITHOUT committing to ship a gallery before there
is organic sharing to populate it. The point of a spike is to make the later
build/no-build decision cheap and informed, not to ship the feature.

## Current state

Single module: `skill_ab_harness.py` (~3715 lines), stdlib-only, Python >=3.11.
Tests: `test_skill_ab_harness.py` (custom runner, NOT pytest; 53 tests pass in
<1s; no `claude`/`git`/network needed). Lint: `uvx ruff check skill_ab_harness.py`
(line-length 100). `__version__ = "0.2.0"` at `skill_ab_harness.py:84` (kept in
sync with `pyproject.toml`).

**STDLIB-ONLY is a hard rule.** Never add a dependency (no numpy/pandas/jinja/
pytest). The HTML report's CSS lives inside a Python triple-quoted string
(`_HTML_STYLE`, starts at `skill_ab_harness.py:1857`); its JS lives inside a raw
triple-quoted r-string of ES5 vanilla JS (`_HTML_SCRIPT`, starts at
`skill_ab_harness.py:2201` — var/function only, no template literals, no
backticks). Comments explain WHY, not what. Keep every line < 100 columns.

### The substrate the gallery ingests

`summary_dict(...)` produces the portable, schema-versioned blob that each run
persists as `summary.json`. It is stamped `schema_version: 2` and carries a
convenience `itt`/`validity` mirror of the PRIMARY pair so simple consumers (badge,
CI) don't have to walk `comparisons`. From `skill_ab_harness.py:1674-1715`:

```python
def summary_dict(results: list[RunResult], cfg: ExperimentConfig, manifest: dict,
                 scorers: list[Scorer] | None = None, seed: int = 0) -> dict:
    """Portable, schema-versioned summary (v2): per-arm validity + every pairwise
    comparison (2-arm: skill vs control; 3-arm: each skill vs control + A vs B). A
    convenience `itt`/`validity` mirror of the PRIMARY pair keeps simple consumers
    (badge, CI) working. Reuses the SAME estimators build_report uses."""
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

`write_summary(...)` (`skill_ab_harness.py:1718-1723`) writes that dict to
`results_dir/summary.json`. The contract is pinned in `results.schema.json`: the
top is `{"schema_version": {"const": 2}, ...}` (`results.schema.json:10`), required
keys are `["schema_version","manifest","primary_metric","primary_pair","arms",
"comparisons"]` (`results.schema.json:7`), and `manifest` requires
`["harness_version","model","skill_name"]` (`results.schema.json:15`).

### The badge code the gallery would reuse to score each entry

`badge_verdict(...)` maps a primary-metric estimate to a self-policing label/color
(grey unless the 95% CI excludes 0 AND the run is trustworthy). From
`skill_ab_harness.py:1768-1783`:

```python
def badge_verdict(est: dict, direction: int, n_tasks: int, clustered: bool,
                  contaminated: int) -> dict:
    """Map a primary-metric estimate to a badge. Self-policing: only claim a
    win/regression when the 95% CI excludes 0 AND the run is trustworthy
    (>=2 clustered tasks, no OFF-arm contamination)."""
    lo, hi, point = est["ci_low"], est["ci_high"], est["point"]
    trustworthy = clustered and n_tasks >= 2 and contaminated == 0
    excludes_zero = not (lo <= 0 <= hi)
    if not trustworthy or not excludes_zero:
        label, color = "inconclusive", "lightgrey"
    else:
        improved = (point > 0) if direction > 0 else (point < 0)
        label = "verified" if improved else "regressed"
        color = "brightgreen" if improved else "red"
    return {"label": label, "color": color, "point": point,
            "ci_low": lo, "ci_high": hi, "n_tasks": n_tasks}
```

`render_badge_svg(...)` emits a flat shields-style SVG with **no external assets/
deps** (`skill_ab_harness.py:1786-1802`). `badge_endpoint_json(...)` emits the
shields.io endpoint schema and `badge_markdown(...)` emits the paste-able badge +
CI comment (`skill_ab_harness.py:1805-1817`).

`primary_verdict(...)` is the existing "summary.json → verdict" adapter, but it
needs an `ExperimentConfig` purely to look up metric direction. From
`skill_ab_harness.py:1820-1827`:

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

`_metric_direction(...)` falls back to `-1` for `cost_usd`/`diff_lines` and `1`
otherwise when no scorer matches (`skill_ab_harness.py:1757-1761`):

```python
def _metric_direction(cfg: ExperimentConfig, metric: str) -> int:
    for s in default_scorers(cfg):
        if s.name == metric:
            return s.direction
    return -1 if metric in ("cost_usd", "diff_lines") else 1
```

**Key constraint for the gallery:** it ingests FOREIGN `summary.json` files from
many runs and has **no `ExperimentConfig`** for any of them. So it cannot call
`primary_verdict`/`_metric_direction` to learn a custom scorer's direction — it can
only use the cfg-free fallback (`-1` for `cost_usd`/`diff_lines`, else `1`). This is
an honest limitation, captured as an open question below.

### The page-rendering pattern to follow (link out, don't embed)

`build_html_report(...)` (`skill_ab_harness.py:3077-3150`) is the per-run dashboard:
it assembles a `window.SKILL_AB` JSON blob, inlines `_HTML_STYLE` and `_HTML_SCRIPT`,
and returns one self-contained string. `cmd_demo(...)`
(`skill_ab_harness.py:3397-3418`) is the closest pattern for a new offline,
stdlib-only, zero-cost renderer: it builds `RunResult`s in code, writes
`report.html` + `summary.json` + `badge.svg` into an out dir, spends nothing. The
CLI dispatch lives in `main(...)` with one `sub.add_parser(...)` per command
(`skill_ab_harness.py:3611-3663`); the `demo` subparser is at
`skill_ab_harness.py:3654-3656` and is dispatched at `skill_ab_harness.py:3662`.

### Test conventions

Tests use helpers in `test_skill_ab_harness.py`: `_cfg(**kw)` (line 18) builds an
`ExperimentConfig`; `_rr(arm, activated, ...)` (line 41) builds a `RunResult`;
`_two_task_results(...)` (line 334) builds a clean 2-task ON/OFF result set.
`test_manifest_and_summary_shape` (line 344) already calls
`h.summary_dict(_two_task_results(), cfg, man)` and asserts the v2 shape — model
new tests on it. The runner at the bottom of the file auto-discovers every
top-level `test_*` callable (`_run_all()`), so a new `def test_...` is picked up
with no registration.

## Commands you will need

| Purpose            | Command                                              | Expected on success            |
|--------------------|------------------------------------------------------|--------------------------------|
| Run tests          | `python3 test_skill_ab_harness.py`                   | ends `N passed` (N ≥ 55), exit 0 |
| Lint               | `uvx ruff check skill_ab_harness.py`                 | `All checks passed!`, exit 0   |
| Smoke the prototype| `python3 -c "import skill_ab_harness as h; print(len(h.build_gallery_html([])))"` | prints a positive integer, exit 0 |

(Run all commands from the repo root `/Users/copyjosh/Code/skills-test`. The repo
is NOT git-initialized — there is no branch/commit workflow; edit files in place.)

## Scope

**In scope** (the only files you may modify):
- `skill_ab_harness.py` — add `build_gallery_html(...)` and the cfg-free verdict
  helper `_gallery_verdict_from_summary(...)`, placed in the badge section right
  AFTER `primary_verdict` (`skill_ab_harness.py:1820-1827`). Pure functions only.
- `test_skill_ab_harness.py` — add the prototype tests.

**Out of scope** (do NOT touch, even though they look related):
- `main(...)` / the argparse subparsers (`skill_ab_harness.py:3603-3711`) — do NOT
  wire a `gallery` CLI subcommand. The command's surface is a DESIGN deliverable in
  this plan, not a build deliverable for this spike. Wiring it (file globbing,
  manifest-of-URLs ingestion, `--out` dir) is explicitly deferred.
- `build_html_report` / `_chart_data` / `_HTML_SCRIPT` / `_HTML_STYLE` — do NOT
  modify them. The prototype REUSES `_HTML_STYLE` by reference (embed it in the
  gallery page) and links OUT to per-run `report.html`; it does not re-render or
  embed a per-run report, and it does not change the existing report.
- `results.schema.json` — do NOT bump or fork the schema in this spike. A gallery
  schema or a `gallery.schema.json` is a follow-up if/when the gallery is built.
- Any networking, file upload, hosting config, or `gh-pages` automation — forbidden
  by the rejected-items list; the spike defines the hosting OPTIONS in prose only.

## Suggested executor toolkit

None required. This is stdlib-only Python; no skills or external docs are needed.
Do not fetch anything from the network.

## Design deliverable (record these decisions in your final report)

This spike must DEFINE the surface without building it. Restate the following
decisions and the open-questions list in your final hand-off summary.

### (a) The `gallery` command surface (design only — not built here)

Proposed signature, consistent with the existing CLI (`init`/`report`/`badge`/
`demo` all take `-o`/`--out` and read local files):

```
skill-ab gallery [SUMMARY ...] [--manifest manifest.json] [-o OUTDIR]
```

- Positional `SUMMARY ...`: zero or more paths to `summary.json` files.
- `--manifest manifest.json`: a static JSON list of entries
  `[{"summary": "<path-or-relative-url>", "report": "<path-or-url|null>",
  "source": "<repo-url|null>"}]`. Local paths are read; remote URLs are NOT
  fetched by the harness (no network) — they are emitted as links the static host
  resolves. This keeps the command offline and side-effect-light.
- Output: a directory containing `index.html` (the gallery) + a copy of each badge
  SVG. The HTML is self-contained (inlines `_HTML_STYLE`); per-run reports are
  linked, never embedded.
- Stdlib-only, offline, zero-cost — mirrors `cmd_demo`'s contract.
- Ingestion rule: skip (don't crash) any entry whose `schema_version != 2`,
  rendering a visible "unsupported schema vN" placeholder card instead.

The PROTOTYPE you build is only the pure `build_gallery_html(entries)` renderer +
verdict helper. The argparse wiring, path/manifest loading, badge-file copying, and
remote-URL handling are deferred to a future build plan.

### (b) Trust / curation / spam model (the load-bearing strategic decision)

`summary.json` is **self-reported**. Be explicit about what is verifiable vs
forgeable:

- **Forgeable**: every numeric field (`point`, `ci_low`, `ci_high`, `p_value`,
  `n_tasks`, validity counts). A malicious or careless author can hand-write any
  `summary.json`; nothing in the schema cryptographically binds the numbers to a
  real run. A leaderboard creates the maximum incentive to forge (ranking = payoff).
- **Partially verifiable (reproducibility, not attestation)**: `manifest`'s
  `skill_md_sha256`, `base_ref_sha`, `model`, `harness_version`, `claude_cli_version`
  let a viewer RE-RUN the experiment (`run --from-github <url>` already shallow-
  clones a repo and runs its committed config — `_clone_from_github`,
  `skill_ab_harness.py:3309`) and check whether the CIs overlap. The harness still
  cannot attest the original numbers came from a real run; reproduction is the only
  real trust anchor.
- **Decision**: the gallery treats every entry as a CLAIM and labels it
  prominently "self-reported, unverified." Curation options, all static / no
  backend / no accounts:
  1. **PR-to-add allowlist**: the gallery's source list is a committed file in a
     repo; adding an entry is a PR a human reviews. (Lowest ops, recommended.)
  2. **"Verified reproduction" tier**: a maintainer re-runs via `run --from-github`
     and marks an entry verified only when the reproduced CI overlaps the claimed
     CI. Surfaced as a separate badge tone, mirroring the existing self-policing
     badge.
  3. **Source pinning**: only list entries whose `manifest` carries a
     `skill_md_sha256` + a public `source` repo URL, so any viewer can reproduce.
- **Non-goal**: do NOT add accounts, an upload endpoint, or a hosted backend — the
  rejected-items list forbids it and it changes the trust model (people won't
  upload private repos' diffs).

### (c) Schema-version / migration policy across many runs

A gallery aggregates `summary.json` files produced over time by different harness
versions. `schema_version` is currently `const: 2` (`results.schema.json:10`).
Policy decisions:
- The gallery pins to the schema versions it understands and **skips** unknown
  versions with a visible placeholder (never crashes). The prototype enforces this
  for `!= 2`.
- When the schema bumps to 3, the gallery needs either a per-version read adapter
  or a documented "shows a reduced card for vN" fallback. Decide ADAPTERS vs
  PIN-TO-LATEST as a follow-up; the spike only records that the gallery must read
  `schema_version` first and branch on it, and that any bump to the schema is a
  cross-cutting change that the gallery (and `badge`/`report` consumers) must track.

### (d) Hosting options (static only — no backend, no accounts)

- **GitHub Pages**: the `gallery` command emits a directory of static files; an
  author commits it to a `gh-pages` branch or `docs/` and Pages serves it. The
  harness does NOT push or automate this (no network, no ops).
- **None / attach-and-DM**: the gallery `index.html` is self-contained like the
  per-run report — attachable to a PR or DM, no host required.
- Either way there is **no server, no database, no accounts** — consistent with the
  rejected "Hosted SaaS / accounts / a results backend" item.

### (e) Concrete reuse points (with anchors)

| Need | Reuse | Anchor |
|------|-------|--------|
| Shape of each ingested entry | `summary_dict` | `skill_ab_harness.py:1674` |
| Contract to validate (`schema_version`, required keys) | `results.schema.json` | `results.schema.json:7,10,15` |
| Score an entry → label/color | `badge_verdict` | `skill_ab_harness.py:1768` |
| Per-entry inline SVG badge (no assets) | `render_badge_svg` | `skill_ab_harness.py:1786` |
| Paste-able badge snippet per entry | `badge_endpoint_json` / `badge_markdown` | `skill_ab_harness.py:1805,1813` |
| Existing summary→verdict adapter (needs cfg) | `primary_verdict` | `skill_ab_harness.py:1820` |
| Direction fallback (cfg-free path only) | `_metric_direction` last line | `skill_ab_harness.py:1761` |
| Page CSS to inline for a consistent look | `_HTML_STYLE` | `skill_ab_harness.py:1857` |
| Per-run report to LINK to (never embed) | `build_html_report` | `skill_ab_harness.py:3077` |
| Offline/zero-cost renderer pattern | `cmd_demo` | `skill_ab_harness.py:3397` |

### Open questions (enumerate in your hand-off; do not resolve them by building)

1. **Direction without a cfg.** A foreign `summary.json` for a custom
   lower-is-better metric (any name other than `cost_usd`/`diff_lines`) would be
   colored as if higher-is-better. Should `summary_dict` start persisting the
   primary metric's direction so the gallery can color correctly? (That is a
   schema-3 change — out of scope here.)
2. **Forgery.** Is reproducibility-on-demand (`run --from-github`) enough, or does
   the gallery need a signed/attested provenance field? What is the minimum that
   stops a fabricated leaderboard entry?
3. **Ranking semantics.** Do entries rank by point estimate, by CI-lower-bound
   (more honest), or only group verified-vs-inconclusive with no ranking at all
   (avoids gaming)?
4. **Cross-metric comparability.** Different runs have different `primary_metric`s;
   can entries even be ranked against each other, or only listed?
5. **Schema migration.** Adapters per version, or pin the gallery to the latest and
   show reduced cards for older summaries?
6. **Is the gallery premature?** It only pays off once there is organic sharing of
   summaries. Should it wait for evidence the viral loop produces shared runs?

## Steps

### Step 1: Add the cfg-free verdict helper `_gallery_verdict_from_summary`

In `skill_ab_harness.py`, immediately AFTER `primary_verdict`
(`skill_ab_harness.py:1820-1827`), add a verdict adapter that works from a
`summary.json` dict alone (no `ExperimentConfig`). It must reuse `badge_verdict`
and use ONLY the cfg-free direction fallback. Target shape:

```python
def _gallery_verdict_from_summary(summary: dict) -> dict | None:
    """Badge verdict for a foreign summary.json WITHOUT a cfg. The gallery ingests
    many summaries and has no ExperimentConfig, so metric direction falls back to
    the same default _metric_direction uses for unknown metrics; a custom lower-is-
    better scorer can't be detected from a summary alone (open question)."""
    metric = summary.get("primary_metric")
    est = (summary.get("itt") or {}).get(metric)
    if not est:
        return None
    direction = -1 if metric in ("cost_usd", "diff_lines") else 1
    return badge_verdict(est, direction, est["n_tasks"], est["clustered"],
                         summary.get("validity", {}).get("off_contaminated", 0))
```

Keep every line < 100 columns; the comment explains WHY (no cfg → fallback), not
what.

**Verify**: `python3 -c "import skill_ab_harness as h; print(h._gallery_verdict_from_summary({'primary_metric':'tests_pass','itt':{'tests_pass':{'point':0.5,'ci_low':0.2,'ci_high':0.8,'n_tasks':2,'clustered':True}},'validity':{'off_contaminated':0}})['label'])"`
→ prints `verified`.

### Step 2: Add the pure renderer `build_gallery_html`

Add `build_gallery_html(entries: list[dict]) -> str` after the helper from Step 1.
Each entry is `{"summary": <summary.json dict>, "report_href": <str|None>}`. The
function must:
- Inline `_HTML_STYLE` (reuse by reference — do NOT copy/duplicate the CSS text)
  for a look consistent with the report.
- For each entry, read `summary["schema_version"]`; if it is not `2`, render a
  visible placeholder card text like `unsupported schema vN` and continue (never
  raise).
- For a v2 entry, compute the verdict via `_gallery_verdict_from_summary`, render
  the inline badge via `render_badge_svg(summary["primary_metric"], verdict)`, show
  the skill name (`summary["manifest"]["skill_name"]`), and link to
  `report_href` when present (`html.escape` all interpolated text).
- Include a prominent, page-level **"self-reported, unverified"** disclaimer (the
  trust decision from (b)) — assert its presence in tests.
- Be a pure function: no file I/O, no network, no globals mutated. Return one HTML
  string.

Skeleton (fill in; keep < 100 cols, ES-free — this is server-rendered HTML, no JS
needed):

```python
def build_gallery_html(entries: list[dict]) -> str:
    """Static index over many runs. entry = {"summary": <summary.json dict>,
    "report_href": <str|None>}. UNVERIFIED by construction: summary.json is self-
    reported, so the page says so. Reuses _HTML_STYLE + render_badge_svg and links
    OUT to each per-run report.html instead of embedding it."""
    cards = []
    for e in entries:
        s = e.get("summary") or {}
        if s.get("schema_version") != 2:
            cards.append(_gallery_unsupported_card(s))   # never crash on old/forged
            continue
        cards.append(_gallery_card(s, e.get("report_href")))
    disclaimer = ("<p class='note'>Every entry below is <b>self-reported</b> and "
                  "<b>unverified</b> — reproduce via <code>skill-ab run "
                  "--from-github</code>.</p>")
    return "".join([
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>skill A/B — gallery</title>",
        f"<style>{_HTML_STYLE}</style></head><body><div class='wrap'>",
        "<h1>skill A/B gallery</h1>", disclaimer,
        "".join(cards), "</div></body></html>",
    ])
```

(You may inline `_gallery_card` / `_gallery_unsupported_card` as small helpers or
fold them into the loop — your call, but keep functions short and lines < 100.)

**Verify**: `python3 -c "import skill_ab_harness as h; html=h.build_gallery_html([]); print('self-report' if 'self-reported' in html else 'MISSING')"`
→ prints `self-report`.

### Step 3: Add the prototype tests (render an index from 2 fixtures)

In `test_skill_ab_harness.py`, add tests that build TWO real `summary.json` dicts
via the existing path and render the gallery. Generate the fixtures with
`h.summary_dict(...)` so no data file is committed and the test stays deterministic
and offline. Model on `test_manifest_and_summary_shape` (line 344). Cover:
- **Happy path (maximal)**: two summaries with different `skill_name`s (use
  `_cfg(skill_name="skill-x")` and `_cfg(skill_name="skill-y")`), both rendered;
  assert the output HTML contains BOTH skill names, the "self-reported" disclaimer,
  and at least one `<svg` badge.
- **Edge — forged/old schema**: an entry whose `summary` is
  `{"schema_version": 1}` (or `99`) renders an "unsupported schema" placeholder and
  does NOT raise.
- **Edge — no estimate**: a v2 summary whose primary metric has no `itt` entry
  yields a verdict of `None` from `_gallery_verdict_from_summary` and the renderer
  still produces a card without crashing.

Sketch:

```python
def test_gallery_renders_two_summaries():
    man = h.experiment_manifest(_cfg(), timestamp=1.0, offline=True)
    sx = h.summary_dict(_two_task_results(), _cfg(skill_name="skill-x"), man)
    sy = h.summary_dict(_two_task_results(), _cfg(skill_name="skill-y"), man)
    out = h.build_gallery_html([{"summary": sx, "report_href": "x/report.html"},
                                {"summary": sy, "report_href": None}])
    assert "skill-x" in out and "skill-y" in out
    assert "self-reported" in out and "<svg" in out


def test_gallery_skips_unsupported_schema():
    out = h.build_gallery_html([{"summary": {"schema_version": 1}}])
    assert "unsupported schema" in out.lower()
```

**Verify**: `python3 test_skill_ab_harness.py` → ends with `N passed` where N is the
prior count (53) plus the number of tests you added (≥ 2), exit 0.

### Step 4: Lint and full-suite gate

**Verify**: `uvx ruff check skill_ab_harness.py` → `All checks passed!`, exit 0;
and `python3 test_skill_ab_harness.py` → all pass, exit 0.

## Test plan

- New tests in `test_skill_ab_harness.py`:
  - `test_gallery_renders_two_summaries` — happy path, two distinct skills both in
    the page, disclaimer present, a badge SVG present.
  - `test_gallery_skips_unsupported_schema` — forged/old `schema_version` → visible
    placeholder, no exception.
  - (Optional) `test_gallery_verdict_none_when_no_estimate` — `itt` missing the
    primary metric → helper returns `None`, renderer still produces a card.
- Structural pattern to follow: `test_manifest_and_summary_shape`
  (`test_skill_ab_harness.py:344`) for building summaries; `_two_task_results`
  (line 334) and `_cfg` (line 18) as fixtures.
- Verification: `python3 test_skill_ab_harness.py` → all pass including the new
  tests; `uvx ruff check skill_ab_harness.py` clean.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `python3 test_skill_ab_harness.py` exits 0 and prints `N passed` with N ≥ 55
      (53 existing + ≥ 2 new), including the new `test_gallery_*` tests.
- [ ] `uvx ruff check skill_ab_harness.py` prints `All checks passed!`, exit 0.
- [ ] `python3 -c "import skill_ab_harness as h; print(len(h.build_gallery_html([])))"`
      prints a positive integer (the empty gallery still renders).
- [ ] `grep -n "def build_gallery_html" skill_ab_harness.py` and
      `grep -n "def _gallery_verdict_from_summary" skill_ab_harness.py` each return
      exactly one match.
- [ ] `grep -n "add_parser(\"gallery\"" skill_ab_harness.py` returns NO matches (no
      CLI subcommand was wired — the spike defers it).
- [ ] No files outside `skill_ab_harness.py` and `test_skill_ab_harness.py` are
      modified (`results.schema.json`, `main(...)`, `build_html_report`, and
      `_HTML_STYLE`/`_HTML_SCRIPT` text are unchanged).
- [ ] Your hand-off summary records: the `gallery` command surface (a), the trust/
      curation model (b), the schema-migration policy (c), the hosting options (d),
      and the open-questions list — i.e. the spike's decisions, not just the code.
- [ ] Your row will be added to `plans/README.md` by the reviewer (you did NOT edit
      it).

## STOP conditions

Stop and report back (do not improvise) if:

- The code at the locations in "Current state" does not match the excerpts —
  e.g. `summary_dict` no longer stamps `schema_version: 2`, `badge_verdict`'s
  signature changed, or `primary_verdict`/`render_badge_svg` moved/changed shape.
  The codebase has drifted since this plan was written; report the diff.
- A verification command fails twice after a reasonable fix attempt.
- Making the prototype work appears to require touching an out-of-scope file
  (`main`, `build_html_report`, `_HTML_STYLE`/`_HTML_SCRIPT`, `results.schema.json`)
  or adding a non-stdlib dependency — either means the scope is wrong; report it.
- You find yourself building the full `gallery` CLI command, manifest/URL loading,
  badge-file copying, or any hosting/network behavior. That is OUT of scope for a
  spike — stop and report that the prototype boundary was reached.
- You discover the assumption "a `summary.json` can be scored to a verdict without
  an `ExperimentConfig`" is false (e.g. `badge_verdict` starts requiring more than
  the fields present in `summary["itt"][metric]` + `validity.off_contaminated`).

## Maintenance notes

For the human/agent who owns this after the spike lands:

- **This is a spike, not a feature.** The decision to actually BUILD the gallery
  (wire the `gallery` CLI, manifest ingestion, badge copying, hosting) is deferred
  and should be a separate plan, gated on evidence the viral loop produces shared
  summaries. Do not let the prototype quietly grow into a half-built gallery — if it
  is not going to be finished, delete `build_gallery_html`/the helper or leave a
  tracked TODO (no orphaned partial work).
- **Direction is the known correctness gap.** `_gallery_verdict_from_summary` colors
  custom lower-is-better metrics wrong. The clean fix is to persist the primary
  metric's direction in `summary.json` — which is a `schema_version` bump (to 3) and
  touches `summary_dict`, `results.schema.json`, and every consumer
  (`primary_verdict`, `badge`, `report`, and this gallery). Treat any schema bump as
  cross-cutting.
- **Trust is the strategic risk, not the code.** A reviewer should scrutinize the
  trust/curation decision (b) hardest: a public leaderboard over self-reported
  numbers maximizes the incentive to forge. Reproducibility (`run --from-github`) is
  the only real anchor today; do not ship ranking without addressing forgery.
- **Honest risk assessment (MED, strategic):** the gallery is the last mile of the
  viral loop, but it is downstream of adoption that may not exist yet, and it
  concentrates the harness's weakest property (self-reported, unverifiable numbers)
  into its most game-able surface (a ranked, public page). The upside is real
  (a destination badges point to) but the spike exists precisely because committing
  to build it now is speculative. Ship the decisions and the tiny prototype; let the
  build wait for demand.
