# Plan 009: Pin the HTML dashboard's Python-side data shaping with tests + cheap JS validation

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, your status row in `plans/README.md`
> will be added by the reviewer who dispatched you — do NOT edit `plans/README.md`
> yourself.
>
> **Drift check (run first)**: this repo is **not git-initialized**, so there is
> no SHA to diff against. Instead, open `skill_ab_harness.py` and
> `test_skill_ab_harness.py` and compare the "Current state" excerpts below
> (each marked with `file:line`) to the live code BEFORE editing. Line numbers
> may have shifted; match on the code text, not the number. If any excerpt no
> longer matches the live code, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: LOW
- **Depends on**: none (synergises with plans 007 and 010 if present, but does not require them)
- **Category**: tests
- **Planned at**: not git-initialized, 2026-06-25

## Why this matters

The interactive `report.html` dashboard is the flagship artifact of this harness,
but the Python that shapes the data it renders — `_chart_data`, `_verdict_blob`,
and the `window.SKILL_AB=...` blob embedded by `build_html_report` — has **no
direct test coverage**. The two existing HTML tests
(`test_build_html_report_renders_and_escapes`, `test_demo_command_writes_offline`)
only check scaffolding (`<html>`, `<table>`) and HTML-escaping; neither ever
parses the embedded JSON blob or asserts its shape. That means a refactor could
silently drop a field the in-page JavaScript depends on (e.g. `comparisons[].itt`
or the `judge` sub-blob), produce a `KeyError` in `_verdict_blob` on an
unexpected badge label, or emit a blob that the vanilla-JS charts can't read —
and every test would still pass. This plan adds deterministic tests that
round-trip the blob through `json.loads`, assert its full shape across 2-arm and
3-arm runs (including a judge-enabled run with a degenerate all-ties pair), and
adds a light structural guard over the embedded JS (optionally `node --check`ed
when `node` is on PATH). It is **tests-only**: no production behavior changes.

## Current state

Single production module `skill_ab_harness.py` (~3715 lines); tests live in
`test_skill_ab_harness.py` and run via a custom stdlib runner (NOT pytest). The
relevant production code:

**`_chart_data` — assembles the JSON the charts render from** (`skill_ab_harness.py:2964-3010`):

```python
def _chart_data(results: list[RunResult], cfg: ExperimentConfig, metrics: list,
                pairs: list, pair_itt: dict, comparisons: list | None) -> dict:
    """Assemble the JSON the in-page charts render from. `pair_itt` is the
    PRECOMPUTED {pair_key: {metric: DiffEstimate|None}} (no re-bootstrapping)."""
    arms = experiment_arms(cfg)
    arm_pal = ["#1f77b4", "#ff7f0e", "#8c8c8c", "#2ca02c", "#d62728"]
    arm_colors = {arm_label(cfg, a): arm_pal[i % len(arm_pal)] for i, a in enumerate(arms)}
    tasks = sorted({r.task_id for r in results})
    pal = ["#4c78a8", "#f58518", "#54a24b", "#e45756", "#72b7b2", "#b279a2"]
    task_colors = {t: pal[i % len(pal)] for i, t in enumerate(tasks)}
    runs = [{"task": r.task_id, "arm": arm_label(cfg, r.arm), "idx": r.run_index,
             "valid": r.itt_valid, "cost": r.cost_usd, "turns": r.num_turns,
             "activated": r.skill_activated, "contam": r.contaminated_by,
             "scores": {m: r.scores.get(m) for m in metrics}} for r in results]
    arm_means = {}
    for a in arms:
        lab = arm_label(cfg, a)
        arm_means[lab] = {}
        for m in metrics:
            vals = [r.scores[m] for r in results
                    if r.arm is a and r.itt_valid and m in r.scores]
            arm_means[lab][m] = statistics.fmean(vals) if vals else None
    comps = []
    for a, b in pairs:
        key = pair_key(cfg, a, b)
        itt = {}
        for m in metrics:
            e = pair_itt[key][m]
            itt[m] = ({"point": e.point, "lo": e.ci_low, "hi": e.ci_high,
                       "p": e.p_value, "q": e.q_value} if e else None)
        comps.append({"key": key, "a": arm_label(cfg, a),
                      "b": arm_label(cfg, b), "itt": itt})
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
            "dir": {m: _metric_direction(cfg, m) for m in metrics},
            "arms": [arm_label(cfg, a) for a in arms], "armColors": arm_colors,
            "tasks": tasks, "taskColors": task_colors, "runs": runs,
            "armMeans": arm_means, "comparisons": comps, "judge": judge}
```

Two shape facts that drive the assertions:
- A `comparisons[]` entry is `{"key", "a", "b", "itt"}`, and each `itt[metric]`
  is either `None` or `{"point","lo","hi","p","q"}` (note the keys are `lo`/`hi`,
  NOT `ci_low`/`ci_high`).
- `win_rate_a` uses the `wr if wr == wr else None` idiom: `aggregate_judge`
  returns `float("nan")` when a pair has no decisive verdicts (all ties), and
  `nan == nan` is `False`, so a degenerate all-ties pair serializes as JSON
  `null` (Python `None`). This is the case to cover explicitly.

**`_verdict_blob` — maps the badge verdict to the hero pill** (`skill_ab_harness.py:3013-3019`):

```python
def _verdict_blob(verdict: dict | None) -> dict:
    """Map the primary-pair badge verdict to the hero status pill (text + tone)."""
    label = verdict["label"] if verdict else "inconclusive"
    text = {"verified": "Significant effect", "regressed": "Regression detected",
            "inconclusive": "Inconclusive at this scale"}[label]
    tone = {"verified": "good", "regressed": "bad", "inconclusive": "flat"}[label]
    return {"label": label, "tone": tone, "text": text}
```

The two `[label]` dict lookups will `KeyError` on any label outside
`{"verified","regressed","inconclusive"}`, and `verdict=None` defaults the label
to `"inconclusive"`. Both behaviors must be pinned.

**`build_html_report` — embeds the blob** (`skill_ab_harness.py:3107-3124, 3147`):

```python
    data = _chart_data(results, cfg, metrics, pairs, pair_itt, comparisons)
    data["verdict"] = _verdict_blob(verdict)
    data["meta"] = {
        "harness": manifest["harness_version"],
        "cli": manifest.get("claude_cli_version") or "?",
        "model": manifest["model"],
        "repo": manifest["repo_path"],
        "baseRef": manifest["base_ref"],
        "sha": (manifest.get("base_ref_sha") or "")[:12],
        "skillMd": (manifest.get("skill_md_sha256") or "")[:16],
        "seed": manifest["seed"],
        "k": manifest.get("k"),
        "alpha": manifest.get("alpha", cfg.bootstrap_alpha),
        "estimand": "ITT",
        "control": arm_label(cfg, Arm.SKILL_OFF),
        "primaryPair": pair_key(cfg, pa, pb),
    }
    blob = json.dumps(data).replace("</", "<\\/")
```

The blob is injected as a single line inside a `<script>` tag, immediately
followed by the JS bundle (`skill_ab_harness.py:3147`):

```python
        f"<script>window.SKILL_AB={blob};\n{_HTML_SCRIPT}</script>",
```

So the embedded JSON sits between the literal `window.SKILL_AB=` and `;\n(function(){`
(`_HTML_SCRIPT` begins with `(function(){` — see `skill_ab_harness.py:2201`:
`_HTML_SCRIPT = r"""(function(){`). The `.replace("</", "<\\/")` turns any `</`
inside the JSON into `<\/`, which is still **valid JSON** (`\/` is a legal escape),
so `json.loads` parses the extracted blob directly with no un-escaping needed.

The embedded JS is a raw ES5 IIFE; the chart/section functions whose presence is
a cheap structural signal that the bundle is intact (`skill_ab_harness.py`):
- `function leadSignal(){` (`:2339`)
- `function barChart(m){` (`:2605`)
- `function forestChart(m){` (`:2656`)
- `function stripChart(m){` (`:2719`)
- `function judgeSection(){` (`:2778`)

**Existing HTML tests — the pattern to extend, and proof they don't inspect the
blob** (`test_skill_ab_harness.py:426-433` and `:499-504`):

```python
def test_build_html_report_renders_and_escapes():
    cfg = _cfg()
    res = _two_task_results()
    res[0].diff = "diff for A <script>alert(1)</script>\n+line"  # must be escaped
    man = h.experiment_manifest(cfg, timestamp=1.0)
    doc = h.build_html_report(res, h.Preflight(), cfg, man)
    assert doc.lstrip().startswith("<") and "</html>" in doc and "<table" in doc
    assert "&lt;script&gt;" in doc and "<script>alert(1)" not in doc
```

```python
def test_demo_command_writes_offline():
    out = Path(tempfile.mkdtemp())
    h.cmd_demo(out, live=False)
    doc = (out / "report.html").read_text()
    assert doc.startswith("<!doctype html") and "</html>" in doc and "<table" in doc
    assert (out / "badge.svg").exists() and "verified" in (out / "badge.svg").read_text()
```

**Test helpers you will reuse** (all already defined in `test_skill_ab_harness.py`):

- `_cfg(**kw)` (`:18-25`) — builds an `ExperimentConfig`; default is a 2-arm config
  (`skill_name="my-skill"`, control labelled `"control"`). Pass
  `skill_b_src=Path("/s/b"), skill_b_name="skill-b"` to get a 3-arm head-to-head.
- `_rr(arm, activated, ...)` (`:41-45`) — builds one `RunResult`.
- `_two_task_results(on_v=1.0, off_v=0.0)` (`:334-341`) — 2 tasks × {SKILL_ON, SKILL_OFF},
  each run carries `scores={"tests_pass": v}`, a `diff`, and `cost_usd=0.05`. This is
  the 2-arm fixture.
- `_three_arm_results()` (`:359-373`) — 2 tasks × {SKILL_ON, SKILL_B, SKILL_OFF} × 3 runs,
  `scores={"tests_pass": v, "lint_pass": 1.0}`, `cost_usd=0.05`. This is the 3-arm fixture;
  use it with the 3-arm `_cfg(...)`. Its arm labels are `skill-a`, `skill-b`, `control`.
- `_cmp(task, pid, ordering, winner, a="my-skill", b="control", pair="my-skill_vs_control")`
  (`:240-243`) — builds a `JudgeComparison` for feeding the `comparisons=` arg of
  `build_html_report`.
- `h.experiment_manifest(cfg, timestamp=1.0)` (offline-safe; pass `offline=True` for
  3-arm to skip git/cli probing, mirroring `test_head_to_head_three_arm_summary` at `:380`).

**`aggregate_judge` degenerate-tie behavior** (`skill_ab_harness.py:1554`): the
returned `win_rate_a` is `a_wins / decisive if decisive else float("nan")`. So a
pair where every verdict is `"tie"` (winner_arm `"tie"`) has `decisive == 0` →
`win_rate_a` is `nan` → `_chart_data` serializes it as `None`. A `JudgeComparison`
with `winner_arm="tie"` is the way to construct this.

### Repo conventions that apply here

- **STDLIB-ONLY is a hard rule.** Do NOT add any dependency (no pytest, numpy,
  pandas, jinja, etc.). Python >= 3.11. New tests use only the stdlib (`re`,
  `json`, `shutil`, `subprocess`, `tempfile`, `pathlib`).
- The test runner is custom (`_run_all` at `test_skill_ab_harness.py:728-734`): it
  collects every top-level `test_*` callable and calls it; a test "passes" by
  returning without raising. **There is no skip mechanism** — to "skip" (e.g. when
  `node` is absent), the test function simply `print(...)`s a note and `return`s
  early; the runner prints `ok` and counts it as passed.
- Comments explain WHY, not what. Match the terse comment style of the
  surrounding tests.
- Lines stay < 100 columns (ruff `line-length = 100`).
- Imports `tempfile` (`:303`) and `json` (`:725`) are already imported mid-file
  with `# noqa: E402`. You may add `import re`, `import shutil`, `import subprocess`
  the same way (mid-file with `# noqa: E402`) near the test functions that use
  them, or place a single new block — match whichever is closer to your new tests.

## Commands you will need

| Purpose   | Command                                          | Expected on success                    |
|-----------|--------------------------------------------------|----------------------------------------|
| Tests     | `python3 test_skill_ab_harness.py`               | ends with `N passed` (currently `53 passed`), exit 0, < 1s |
| Lint      | `uvx ruff check skill_ab_harness.py`             | see Done criteria (pre-existing baseline noted below) |
| Lint test | `uvx ruff check test_skill_ab_harness.py`        | see Done criteria                      |
| node check| `command -v node`                                | prints a path if node exists, else nothing |

Run all commands from the repo root `/Users/copyjosh/Code/skills-test`.

**Pre-existing lint baseline (NOT introduced by you, OUT OF SCOPE to fix):**
`uvx ruff check skill_ab_harness.py` currently reports exactly **4 `E702`**
(multiple-statements-on-one-line / semicolon) errors at lines 1158, 1159, 3661,
3663. `uvx ruff check test_skill_ab_harness.py` currently reports exactly **9
`E702`** errors. These are the repo's existing style (semicolons are used
liberally) and are unrelated to this plan. Your job is to add **zero new** ruff
errors, not to fix these.

## Scope

**In scope** (the only file you should modify):
- `test_skill_ab_harness.py` — add new test functions only.

**Out of scope** (do NOT touch):
- `skill_ab_harness.py` — this is a tests-only plan. Do NOT change `_chart_data`,
  `_verdict_blob`, `build_html_report`, `_HTML_SCRIPT`, or anything else in the
  production module. If a test reveals a real bug in production code, that is a
  STOP condition (report it; do not fix it here).
- The pre-existing `E702` lint findings (lines 1158/1159/3661/3663 in the prod
  file, and the 9 in the test file) — leave them.
- `plans/README.md` and `plans/001`–`plans/008` — the reviewer maintains the index.

## Git workflow

This repo is **not git-initialized** (no `.git`, no branches). Just edit
`test_skill_ab_harness.py` in place. Do not run `git` commands, do not create a
branch, do not commit.

## Steps

### Step 1: Add a blob-extraction helper and a 2-arm shape test

Near the existing HTML tests (after `test_build_html_report_renders_and_escapes`,
around `test_skill_ab_harness.py:434`), add a small helper that pulls the embedded
JSON out of a rendered doc, plus the first test. Add `import re` mid-file with
`# noqa: E402` if `re` is not already imported.

Helper (the extraction is anchored on the literal injection boundary — the blob
is one `json.dumps` line between `window.SKILL_AB=` and `;\n(function(){`; the
extracted text is already valid JSON despite the `</`→`<\/` replacement, so
`json.loads` parses it directly):

```python
def _skill_ab_blob(doc: str) -> dict:
    m = re.search(r"window\.SKILL_AB=(.+?);\n\(function\(\)\{", doc, re.S)
    assert m, "window.SKILL_AB blob not found / injection boundary changed"
    return json.loads(m.group(1))   # already valid JSON (\/ escape is legal)
```

Then the 2-arm test:

```python
def test_html_blob_shape_two_arm():
    cfg = _cfg()
    man = h.experiment_manifest(cfg, timestamp=1.0)
    data = _skill_ab_blob(h.build_html_report(_two_task_results(), h.Preflight(), cfg, man))
    # top-level keys the in-page JS reads
    assert set(data) >= {"metrics", "primary", "dir", "arms", "armColors", "tasks",
                         "taskColors", "runs", "armMeans", "comparisons", "judge",
                         "verdict", "meta"}
    assert data["primary"] == "tests_pass"
    assert set(data["arms"]) == {"my-skill", "control"}
    assert set(data["armColors"]) == {"my-skill", "control"}
    assert data["meta"]["estimand"] == "ITT" and data["meta"]["primaryPair"]
    assert data["meta"]["control"] == "control"
    # per-run rows carry the fields the strip chart needs
    row = data["runs"][0]
    assert set(row) >= {"task", "arm", "idx", "valid", "cost", "turns",
                       "activated", "contam", "scores"}
    # armMeans is per-arm per-metric
    assert "tests_pass" in data["armMeans"]["my-skill"]
    # exactly one comparison (skill vs control); itt uses lo/hi (NOT ci_low/ci_high)
    assert len(data["comparisons"]) == 1
    c = data["comparisons"][0]
    assert {"key", "a", "b", "itt"} <= set(c)
    d = c["itt"]["tests_pass"]
    assert set(d) == {"point", "lo", "hi", "p", "q"}
    assert data["judge"] == []   # no comparisons passed -> empty judge sub-blob
```

**Verify**: `python3 test_skill_ab_harness.py` → ends with a higher `N passed`
count and exit 0; the line `ok  test_html_blob_shape_two_arm` appears.

### Step 2: Add a 3-arm (head-to-head) shape test

Reuse the existing `_three_arm_results()` fixture and a 3-arm config. Assert all
three pairwise comparisons appear in the blob and the primary pair is A vs B.

```python
def test_html_blob_shape_three_arm():
    cfg = _cfg(skill_name="skill-a", skill_b_src=Path("/s/b"), skill_b_name="skill-b")
    man = h.experiment_manifest(cfg, timestamp=1.0, offline=True)
    data = _skill_ab_blob(
        h.build_html_report(_three_arm_results(), h.Preflight(), cfg, man))
    assert set(data["arms"]) == {"skill-a", "skill-b", "control"}
    keys = {c["key"] for c in data["comparisons"]}
    assert keys == {"skill-a_vs_control", "skill-b_vs_control", "skill-a_vs_skill-b"}
    assert data["meta"]["primaryPair"] == "skill-a_vs_skill-b"
    # every comparison carries an itt for the primary metric
    for c in data["comparisons"]:
        assert "tests_pass" in c["itt"]
```

**Verify**: `python3 test_skill_ab_harness.py` → passes; `ok  test_html_blob_shape_three_arm`
appears.

### Step 3: Add a judge-enabled test covering the degenerate all-ties pair

Pass `comparisons=` to `build_html_report` (a list of `JudgeComparison`s built via
`_cmp`). Include one pair with a clear A-preference and one pair where every
verdict is a tie, and assert the tie pair serializes `win_rate_a` as JSON `null`
(Python `None`).

```python
def test_html_blob_judge_subblob_and_all_ties():
    cfg = _cfg()
    man = h.experiment_manifest(cfg, timestamp=1.0)
    comps = [
        # decisive pair: A preferred in both orderings
        _cmp("t", 0, "a_first", "my-skill"), _cmp("t", 0, "b_first", "my-skill"),
        # degenerate pair: every verdict a tie -> no decisive -> win_rate_a null
        _cmp("t", 1, "a_first", "tie"), _cmp("t", 1, "b_first", "tie"),
    ]
    data = _skill_ab_blob(
        h.build_html_report(_two_task_results(), h.Preflight(), cfg, man, comparisons=comps))
    j = data["judge"]
    assert len(j) == 1                       # one pair key: my-skill_vs_control
    e = j[0]
    assert {"pair", "a", "b", "a_wins", "b_wins", "ties", "win_rate_a",
            "consistency"} <= set(e)
    # 2 decisive A-wins + 2 ties across the two pair_ids; ties contribute no decision
    assert e["a_wins"] == 2 and e["ties"] == 2
    assert e["win_rate_a"] == 1.0            # decisive verdicts all favor A
```

If `aggregate_judge` happens to split these into separate pair entries in your
build (it groups by the `pair` key, which is `"my-skill_vs_control"` for all four
`_cmp` rows above, so they aggregate into ONE entry), the `len(j) == 1` assertion
holds. To ALSO directly exercise the `win_rate_a is None` branch, add a second
tiny test with ONLY tie verdicts:

```python
def test_html_blob_judge_win_rate_null_when_no_decisive():
    cfg = _cfg()
    man = h.experiment_manifest(cfg, timestamp=1.0)
    comps = [_cmp("t", 0, "a_first", "tie"), _cmp("t", 0, "b_first", "tie")]
    data = _skill_ab_blob(
        h.build_html_report(_two_task_results(), h.Preflight(), cfg, man, comparisons=comps))
    assert data["judge"][0]["win_rate_a"] is None   # nan -> JSON null
```

**Verify**: `python3 test_skill_ab_harness.py` → both new tests appear as `ok`
and the suite passes.

### Step 4: Pin `_verdict_blob` directly (label → tone/text, and no KeyError)

Call `h._verdict_blob` directly for each known label and for `None`. This guards
the brittle dict lookups at `skill_ab_harness.py:3016-3018`.

```python
def test_verdict_blob_maps_each_label():
    cases = {"verified": ("good", "Significant effect"),
             "regressed": ("bad", "Regression detected"),
             "inconclusive": ("flat", "Inconclusive at this scale")}
    for label, (tone, text) in cases.items():
        b = h._verdict_blob({"label": label})
        assert b == {"label": label, "tone": tone, "text": text}
    # None verdict defaults to inconclusive (no KeyError)
    assert h._verdict_blob(None)["label"] == "inconclusive"
```

**Verify**: `python3 test_skill_ab_harness.py` → `ok  test_verdict_blob_maps_each_label`.

### Step 5: Add the light JS structural guard (function names present + optional `node --check`)

Add `import shutil` and `import subprocess` mid-file with `# noqa: E402` if not
already imported. The test asserts the known JS function names are present in the
rendered doc, then — ONLY if `node` is on PATH — extracts the full `<script>` body
and runs `node --check` on it; otherwise it prints a note and returns (the custom
runner counts that as a pass). `node --check` only parses (does not execute), so
`window.SKILL_AB=...` referencing an undefined `window` is fine.

```python
def test_html_script_structural_and_optional_node_check():
    cfg = _cfg()
    man = h.experiment_manifest(cfg, timestamp=1.0)
    doc = h.build_html_report(_two_task_results(), h.Preflight(), cfg, man)
    for fn in ("function leadSignal(", "function barChart(", "function forestChart(",
               "function stripChart(", "function judgeSection("):
        assert fn in doc, f"missing JS function: {fn}"
    # extract the <script> body (the blob assignment + the IIFE bundle)
    m = re.search(r"<script>(.+?)</script>", doc, re.S)
    assert m, "script tag not found"
    script = m.group(1)
    node = shutil.which("node")
    if not node:
        print("    (node not on PATH; skipping node --check)")
        return
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(script)
        path = f.name
    proc = subprocess.run([node, "--check", path], capture_output=True, text=True)
    assert proc.returncode == 0, f"node --check failed:\n{proc.stderr}"
```

Note: the rendered doc contains exactly one `<script>` block (the bundle); the
non-greedy `(.+?)` with `re.S` captures it. If a future change adds a second
`<script>`, this regex grabs the first — acceptable for this guard.

**Verify**: `python3 test_skill_ab_harness.py` → `ok  test_html_script_structural_and_optional_node_check`
appears (with the skip note printed if `node` is absent). Optionally confirm both
paths: run once as-is, and if you have `node`, confirm no skip note prints.

### Step 6: Final lint + full-suite check

Confirm no new ruff errors and the whole suite passes.

**Verify**:
- `uvx ruff check test_skill_ab_harness.py` → still exactly **9 `E702`** and
  nothing else (you added no new violation; do not use semicolons-on-one-line in
  your new code).
- `uvx ruff check skill_ab_harness.py` → still exactly **4 `E702`** (unchanged;
  you did not touch the prod file).
- `python3 test_skill_ab_harness.py` → ends with `N passed` where N is the old
  count plus the number of new tests (6 new tests if you added all of Steps 1–5),
  exit 0.

## Test plan

New tests, all in `test_skill_ab_harness.py`, modeled after the existing HTML
tests (`test_build_html_report_renders_and_escapes` at `:426`) and reusing
`_cfg` / `_two_task_results` / `_three_arm_results` / `_cmp`:

- `test_html_blob_shape_two_arm` — happy path: 2-arm blob has all top-level keys,
  one comparison, `itt` uses `lo`/`hi`, empty `judge`.
- `test_html_blob_shape_three_arm` — 3-arm head-to-head: all three pairwise
  comparison keys present, primary pair is A vs B.
- `test_html_blob_judge_subblob_and_all_ties` — judge-enabled run; judge sub-blob
  shape; decisive-pair win rate.
- `test_html_blob_judge_win_rate_null_when_no_decisive` — edge case: all-ties pair
  serializes `win_rate_a` as `None`.
- `test_verdict_blob_maps_each_label` — each badge label → tone/text; `None`
  defaults to inconclusive without `KeyError`.
- `test_html_script_structural_and_optional_node_check` — structural guard: JS
  function names present; optional `node --check` (skips cleanly when node absent).

Plus the `_skill_ab_blob(doc)` extraction helper.

Verification: `python3 test_skill_ab_harness.py` → all pass, including the 6 new
tests; total goes from `53 passed` to `59 passed`.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `python3 test_skill_ab_harness.py` exits 0 and ends with `59 passed`
      (53 pre-existing + 6 new); the 6 new `ok  test_html_*` / `ok  test_verdict_blob_*`
      lines appear.
- [ ] `uvx ruff check skill_ab_harness.py` reports **exactly the 4 pre-existing
      `E702`** (lines 1158, 1159, 3661, 3663) and no other error — i.e. unchanged,
      because this plan does not modify the production module.
- [ ] `uvx ruff check test_skill_ab_harness.py` reports **exactly the 9
      pre-existing `E702`** and no NEW error (your added test code introduces no
      new ruff violation).
- [ ] `skill_ab_harness.py` is byte-for-byte unchanged (tests-only plan).
- [ ] No new third-party dependency added (stdlib-only); `pyproject.toml`
      `dependencies = []` unchanged.

## STOP conditions

Stop and report back (do not improvise) if:

- Any "Current state" excerpt no longer matches the live code (the report
  pipeline drifted) — in particular if the `window.SKILL_AB=...;\n(function(){`
  injection boundary at `skill_ab_harness.py:3147` changed, or `_chart_data`'s
  returned keys / `itt` key names (`lo`/`hi`) changed, or `_verdict_blob`'s label
  set changed. The extraction regex and shape assertions depend on these exact
  forms.
- A new test fails because the production code does something the plan claims it
  does not (e.g. `_verdict_blob` raises on a known label, or `win_rate_a` is NOT
  `None` for an all-ties pair). That is a real production bug — report it; do NOT
  fix `skill_ab_harness.py` in this tests-only plan.
- `uvx ruff check skill_ab_harness.py` shows MORE than the 4 documented `E702`
  errors before you start (the prod file drifted) — recheck the baseline and
  report.
- A step's verification fails twice after a reasonable fix attempt.
- Implementing any step appears to require editing `skill_ab_harness.py` or any
  file outside `test_skill_ab_harness.py`.

## Maintenance notes

For whoever owns the report after this lands:

- These tests hard-code the embedded-blob injection boundary (`window.SKILL_AB=`
  ... `;\n(function(){`) and the `itt` key names `lo`/`hi`. If you rename those or
  change how the blob is embedded in `build_html_report`, update `_skill_ab_blob`
  and the shape assertions together — the tests are intentionally coupled to the
  contract the in-page JS reads.
- The `node --check` guard is best-effort: it only runs where `node` is installed
  (skipped silently otherwise), so CI without node still passes. If you want it to
  be mandatory, gate it behind an env var rather than removing the skip — the
  stdlib-only / no-required-node-toolchain rule is deliberate.
- A reviewer should scrutinize that the new tests assert the blob's *shape*
  (keys/contract the JS depends on), not just that *some* JSON parses — a test
  that only `json.loads`es without key assertions would not catch a dropped field.
- Deferred out of scope: validating the JS's runtime *behavior* (rendering the
  charts in a headless browser) — that needs a browser/JS runtime and is a much
  heavier dependency than this harness's stdlib-only design allows. This plan
  stops at parse-level (`node --check`) validation. Your status row in
  `plans/README.md` is added by the reviewer.
