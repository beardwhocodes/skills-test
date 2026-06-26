# Plan 014: Stop the verdict hero `<h1>` from asserting a settled effect while the pill says "Inconclusive"

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, your status row in `plans/README.md`
> will be added by the reviewer who dispatched you — do NOT edit `plans/README.md`
> yourself.
>
> **Drift check (run first)**: this repo is **not git-initialized**, so there is
> no SHA to diff against. Before editing, compare the "Current state" excerpts
> below to the live code in `skill_ab_harness.py` (open the cited line ranges and
> confirm the quoted lines still match). On any mismatch, treat it as a STOP
> condition.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug (honesty / UX)
- **Planned at**: not git-initialized, 2026-06-25

## Why this matters

The HTML report's two most prominent elements can flatly contradict each other.
The status **pill** (top-left of the hero card) is driven by `badge_verdict`,
which returns `"inconclusive"` whenever the run is not trustworthy — including the
common single-task smoke test (`n_tasks < 2`). But the big hero **`<h1>` headline**
is computed by a *different* path (`leadSignal` → `leadIn`) that picks the most
decisive `(pair, metric)` purely on "the 95% CI does not straddle 0" plus the
biggest relative gap — it never consults trustworthiness. So on a 1-task run the
pill reads "Inconclusive at this scale" while the headline proclaims e.g.
"my-skill lifted Tests pass by +100%" or "my-skill ran ~35% cheaper per run" as a
settled fact. A reader trusts the big number; the report undercuts its own
honesty guarantee (the badge is explicitly "self-policing" — see
`badge_verdict`'s docstring). This is the current behavior, confirmed by a live
build (single task, ON `tests_pass=1.0` vs OFF `0.0` → verdict label
`inconclusive`, yet the primary comparison CI is `[1.0, 1.0]`, which does not
straddle 0, so the headline fires).

After this plan: when the verdict is inconclusive, the headline no longer
presents a metric difference as a settled claim. It either falls back to neutral
copy or is clearly marked suggestive, while the existing honest "smoke test, not
a verdict" lede text is preserved.

## Current state

Single module under change: `/Users/copyjosh/Code/skills-test/skill_ab_harness.py`
(~3715 lines). All the report UI is Python string literals near the bottom:
`_HTML_STYLE` (a triple-quoted CSS str) and `_HTML_SCRIPT` (a **raw** triple-quoted
`r"""..."""` block of **ES5 vanilla JS** — `var`/`function`, no template literals,
no backticks, no arrow functions; every line stays **< 100 columns**). The JS runs
in the browser against a `window.SKILL_AB` JSON blob; the stdlib test runner cannot
execute it, so tests assert on substrings of the produced document.

### The two contradicting paths

**(1) The pill** comes from `badge_verdict` (returns `"inconclusive"` unless
clustered AND `n_tasks >= 2` AND `contaminated == 0`) — `skill_ab_harness.py:1768`:

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
```

That label is mapped to the pill text/tone by `_verdict_blob`
(`skill_ab_harness.py:3013`), which is what the hero pill renders:

```python
def _verdict_blob(verdict: dict | None) -> dict:
    """Map the primary-pair badge verdict to the hero status pill (text + tone)."""
    label = verdict["label"] if verdict else "inconclusive"
    text = {"verified": "Significant effect", "regressed": "Regression detected",
            "inconclusive": "Inconclusive at this scale"}[label]
    tone = {"verified": "good", "regressed": "bad", "inconclusive": "flat"}[label]
    return {"label": label, "tone": tone, "text": text}
```

**(2) The headline** comes from `leadSignal`/`leadIn`, which ignore trustworthiness
and fire whenever a CI does not straddle 0 — `skill_ab_harness.py:2323` (inside the
`_HTML_SCRIPT` JS string):

```javascript
  function leadIn(comps, onlyMetric){
    var best = null;
    comps.forEach(function(c){
      (onlyMetric ? [onlyMetric] : metricsWithData()).forEach(function(m){
        var d = c.itt && c.itt[m];
        if (!d || !hasData(m) || straddles(d)) return;
        var base = Math.abs(mean(c.b, m)) || Math.abs(d.point) || 1;
        var rel = Math.abs(d.point) / base;
        if (!best || rel > best.rel) best = { c:c, m:m, d:d, rel:rel };
      });
    });
    return best;
  }
  /* ...comment... */
  function leadSignal(){
    var comps = D.comparisons || [];
    var prim = comps.filter(function(c){ return c.key === META.primaryPair; });
    return leadIn(prim, D.primary) || leadIn(prim) || leadIn(comps);
  }
```

`straddles` is `function straddles(c){ return c.lo <= 0 && c.hi >= 0; }`
(`skill_ab_harness.py:2273`). `VERD` is read once near the top of the script —
`skill_ab_harness.py:2205`:

```javascript
  var VERD = D.verdict || {label:"inconclusive", tone:"flat",
    text:"Inconclusive at this scale"};
```

The **hero()** function builds the headline — `skill_ab_harness.py:2417`. This is
the block you will edit:

```javascript
  function hero(){
    var lead = leadSignal();
    var ledCol = qualVar(VERD.tone === "good" ? "good"
      : VERD.tone === "bad" ? "bad" : "flat");
    var headline, sentence;
    if (lead){
      var good = quality(lead.m, lead.d.point) === "good";
      var better = good ? lead.c.a : lead.c.b, worse = good ? lead.c.b : lead.c.a;
      var mb = mean(better, lead.m), mw = mean(worse, lead.m);
      var accent = "<span class='accent' style='color:" + color(better) + "'>"
        + esc(better) + "</span>";
      if (lead.m === "cost_usd"){
        var p = mw ? Math.round((mw - mb) / mw * 100) : 0;
        headline = accent + " ran ~" + p + "% cheaper per run";
      } else if (dirOf(lead.m) < 0){
        var p2 = mw ? Math.round((mw - mb) / mw * 100) : 0;
        headline = accent + " produced ~" + p2 + "% smaller "
          + metricLabel(lead.m).toLowerCase();
      } else {
        headline = accent + " lifted " + metricLabel(lead.m).toLowerCase()
          + " by " + fmtSigned(lead.m, mb - mw);
      }
      sentence = "The clearest signal is <b>" + esc(metricLabel(lead.m))
        + "</b>: " + esc(better) + " at <b>" + fmtVal(lead.m, mb) + "</b> vs "
        + esc(worse) + " at <b>" + fmtVal(lead.m, mw)
        + "</b> (95% CI clears 0). ";
    } else {
      headline = "No metric separated the arms at this scale";
      sentence = "No metric's 95% interval cleared zero. ";
    }
```

Further down, `hero()` already builds an honest `smoke` lede (keep it intact) —
`skill_ab_harness.py:2458`:

```javascript
    var smoke = nTasks < 2
      ? "With " + nTasks + " task" + (k != null ? " and k=" + k : "")
        + ", read this as a <b>smoke test, not a verdict</b>."
      : "";
```

…and emits the headline + lede here — `skill_ab_harness.py:2499`:

```javascript
      + "<h1>" + headline + "</h1>"
      + "<p class='lede'>" + sentence + judgeSentence + smoke + "</p>"
```

`nTasks` is in scope: `var nTasks = (D.tasks || []).length;`
(`skill_ab_harness.py:2299`). `VERD.label` is the canonical "is this trustworthy"
signal and is already in scope. There is **no** contamination count exposed to the
JS blob today — `data["meta"]` (built in `build_html_report`,
`skill_ab_harness.py:3109`) carries `harness/cli/model/repo/.../primaryPair` but not
`total_contam`; `total_contam` is computed server-side at
`skill_ab_harness.py:3089` (`total_contam = sum(1 for r in results if r.contaminated)`).

### Why a single-task run trips this (the bug repro)

For one task, the cluster bootstrap takes the degenerate (non-clustered) path and
resamples runs within that task. With ON runs all `1.0` and OFF all `0.0`, every
bootstrap draw is `1.0`, so the primary CI is `[1.0, 1.0]` — it does **not**
straddle 0, so `leadSignal` returns a hit and the headline asserts a +100% lift.
Meanwhile `badge_verdict` returns `inconclusive` because `n_tasks < 2`. Confirmed
by a live `build_html_report` run (verdict label `inconclusive`, primary
comparison `tests_pass` CI `[1.0, 1.0]`).

### Conventions that apply here

- ES5 only inside `_HTML_SCRIPT`: `var`/`function`, string concatenation with `+`,
  no template literals/backticks, no arrow functions. Keep every line **< 100 cols**.
- HTML you concatenate into `headline`/`sentence` must be escaped where it is
  user-derived; the existing code uses `esc(...)` for arm names — reuse it. Plain
  static copy needs no escaping.
- Comments explain **why**, not what.
- The non-ASCII glyphs already used in this file (`−`, `·`, `…`, `≈`, em dash `—`)
  are fine to reuse; the source file is UTF-8.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Tests   | `python3 test_skill_ab_harness.py` | prints `ok  <name>` per test, ends `N passed`, exit 0 |
| Lint    | `uvx ruff check skill_ab_harness.py` | `All checks passed!`, exit 0 |

Run both from the repo root `/Users/copyjosh/Code/skills-test`. The test runner is
a **custom stdlib runner, NOT pytest** — do not invoke pytest, do not add any
dependency. Python is >= 3.11.

There are currently **53 tests** passing in well under 1 second with no
`claude`/`git`/network needed. After this plan there must be **54** (your new test).

## Scope

**In scope** (the only file you modify):
- `/Users/copyjosh/Code/skills-test/skill_ab_harness.py` — the `hero()` JS in
  `_HTML_SCRIPT`; optionally `_verdict_blob` and the `data["meta"]` dict if you do
  the optional enhancement in Step 2.
- `/Users/copyjosh/Code/skills-test/test_skill_ab_harness.py` — add one test.

**Out of scope** (do NOT touch, even though they look related):
- `badge_verdict`, `cluster_bootstrap_ci`, `estimate_diff`, `leadSignal`/`leadIn`,
  `straddles` — the statistics and selection logic are correct; this is a
  presentation-honesty fix in `hero()` only. Changing the estimator would regress
  the documented ITT estimand.
- `render_badge_svg` and the SVG badge — already self-policing.
- `plans/README.md` — the dispatching reviewer maintains the index.
- Any other plan file (`001`–`006`, `011`).
- **Hard rule: never add a dependency** (no numpy/pandas/jinja/pytest). Stdlib only.

## Git workflow

The repo is **not git-initialized**. There is no branch to cut and no commit to
make — just edit the files in place and leave them saved. Do not run `git`.

## Steps

### Step 1: Gate the assertive headline on `VERD.label`

In `hero()` (`skill_ab_harness.py:2417`), make the headline honest when the verdict
is not trustworthy. The cleanest minimal change: compute a boolean once, and when
the verdict is inconclusive, either (a) skip the assertive headline entirely in
favor of the neutral copy already used in the `else` branch, or (b) keep the number
but prefix a clear suggestive marker so it does not read as settled.

Use approach (b) so the smoke-test reader still sees the magnitude but cannot
mistake it for a verdict. Introduce a single new literal marker **`"Suggestive only — "`**
(em dash, matching the file's existing em-dash usage) that is prepended to the
headline whenever `VERD.label === "inconclusive"` AND a `lead` exists. Keep the
honest `smoke` lede untouched.

Target shape (ES5, < 100 cols) — adapt to the exact surrounding code:

```javascript
  function hero(){
    var lead = leadSignal();
    var settled = VERD.label !== "inconclusive";   // pill is the source of truth
    var ledCol = qualVar(VERD.tone === "good" ? "good"
      : VERD.tone === "bad" ? "bad" : "flat");
    var headline, sentence;
    if (lead && settled){
      /* ...existing assertive headline branch, unchanged... */
    } else if (lead){
      /* CI clears 0 but the run isn't trustworthy (e.g. n_tasks<2): show the
         signal, but mark it suggestive so it can't be read as a verdict. */
      var accent2 = "<span class='accent' style='color:" + color(lead.c.a)
        + "'>" + esc(lead.c.a) + "</span>";
      headline = "Suggestive only — " + accent2 + " vs " + esc(lead.c.b)
        + " on " + esc(metricLabel(lead.m)) + " (not a verdict at this scale)";
      sentence = "A metric's 95% interval cleared zero, but the run isn't "
        + "trustworthy yet, so treat the gap as a lead, not a result. ";
    } else {
      headline = "No metric cleared the bar at this scale";
      sentence = "No metric's 95% interval cleared zero. ";
    }
```

Notes:
- Keep the existing assertive branch body **exactly as-is**; only wrap its
  condition with `&& settled` and add the new `else if (lead)` branch.
- The neutral `else` headline copy was `"No metric separated the arms at this
  scale"`. You may keep that exact string or switch to `"No metric cleared the bar
  at this scale"`; either is fine, but pick one and make the test assert the one
  you ship.
- Do not reference `lead.d.point`/`mb`/`mw` in the suggestive branch unless you
  recompute them there — the assertive branch's locals are scoped to that branch.
- Stay < 100 cols on every line. Use `esc(...)` for `lead.c.a` / `lead.c.b` /
  metric label (arm names are user-derived).

**Verify**: `uvx ruff check skill_ab_harness.py` → `All checks passed!`
(ruff won't parse the JS, but it confirms you didn't break the Python string/quotes
— an unterminated triple-quote would surface as a syntax error here). Then
`python3 test_skill_ab_harness.py` → still ends `53 passed` (no behavior the
existing tests assert on should change yet).

### Step 2 (OPTIONAL — only if trivially clean): name *why* it's inconclusive

If and only if you can do it without touching the estimator, expose the
contamination count so the lede can distinguish "underpowered" (`n_tasks < 2`) from
"contaminated". In `build_html_report` (`skill_ab_harness.py:3109`), add
`"totalContam": total_contam,` to the `data["meta"]` dict (`total_contam` is already
computed at line 3089). Then in the suggestive branch you may append, in ES5:

```javascript
      if ((META.totalContam || 0) > 0)
        sentence += "A foreign skill fired in this run, so exposure is invalid. ";
      else if (nTasks < 2)
        sentence += "Only " + nTasks + " task — too little to cluster. ";
```

If this adds any awkwardness or risks the < 100-col rule, **skip Step 2 entirely** —
it is not required for Done. Do not over-engineer.

**Verify**: `python3 test_skill_ab_harness.py` → still `53 passed`.

### Step 3: Add a regression test

Add one test to `test_skill_ab_harness.py`, in the "HTML report" section near
`test_build_html_report_renders_and_escapes` (`test_skill_ab_harness.py:426`). Model
its construction on `test_single_task_badge_warning`
(`test_skill_ab_harness.py:714`) for the single-task setup and on
`test_build_html_report_renders_and_escapes` for calling `build_html_report`.

The test builds a **1-task** run set whose primary CI excludes 0, asserts the
verdict blob is `inconclusive`, and asserts the document does **not** present the
headline as a settled claim (the new suggestive marker is present, and the
assertive cost/lift phrasing is **absent** for this data). Extract the embedded
JSON blob to read the verdict label robustly.

```python
def test_inconclusive_verdict_does_not_assert_settled_headline():
    # 1 task, ON tests_pass=1.0 vs OFF=0.0 -> primary CI [1.0,1.0] (clears 0),
    # but n_tasks<2 so badge_verdict -> "inconclusive". The hero headline must
    # NOT read as a settled claim when the pill says inconclusive.
    cfg = _cfg()
    res = []
    for arm, v in ((Arm.SKILL_ON, 1.0), (Arm.SKILL_OFF, 0.0)):
        for i in range(3):
            r = _rr(arm, arm is Arm.SKILL_ON, scores={"tests_pass": v})
            r.task_id = "only"; r.run_index = i; r.cost_usd = 0.05
            res.append(r)
    man = h.experiment_manifest(cfg, timestamp=1.0, offline=True)
    doc = h.build_html_report(res, h.Preflight(), cfg, man)

    m = re.search(r"window\.SKILL_AB=(\{.*?\});\n", doc, re.S)
    assert m, "embedded SKILL_AB blob not found"
    data = json.loads(m.group(1))
    assert data["verdict"]["label"] == "inconclusive"
    # primary comparison CI clears 0 -> the bug condition is actually present
    prim = next(c for c in data["comparisons"] if c["key"] == data["meta"]["primaryPair"])
    lo = prim["itt"]["tests_pass"]["lo"]
    assert lo > 0, "test setup must make the primary CI exclude 0"

    # the honest pill text is still there
    assert "Inconclusive at this scale" in doc
    # and the hero now ships the suggestive marker copy (the fix)
    assert "Suggestive only" in doc
```

Notes for the executor:
- `re` and `json` must be importable in the test module. `random` and `Path` are
  imported at the top; `json` is imported at `test_skill_ab_harness.py:725` (bottom,
  with `# noqa: E402`) and `re` may not be imported yet. Put `import re` and (if
  missing) `import json` at the **top** of the file with the other imports
  (`test_skill_ab_harness.py:9-15`) to avoid an `E402`/`NameError`; if `json` is
  already imported at the bottom, leaving it there is fine since the runner imports
  the whole module before running any test. Prefer adding `import re` up top.
- `_cfg` and `_rr` are existing helpers (`test_skill_ab_harness.py:18` and `:41`).
  `_rr` sets `arm_skill_name` correctly per arm; you only override `task_id`,
  `run_index`, `cost_usd`.
- The assertion `"Suggestive only" in doc` is a **source-presence** check: the JS
  is embedded verbatim, so this confirms the new branch shipped. Combined with the
  blob's `inconclusive` label and the `lo > 0` precondition, it pins the exact
  contradiction this plan fixes. (The stdlib runner cannot execute the JS, so a
  rendered-DOM assertion is not possible — this mirrors how
  `test_build_html_report_renders_and_escapes` asserts on doc substrings.)
- If you changed the neutral `else` headline string in Step 1, that branch is not
  exercised by this data; no assertion needed on it.

**Verify**: `python3 test_skill_ab_harness.py` → ends `54 passed`, including
`ok  test_inconclusive_verdict_does_not_assert_settled_headline`.

## Test plan

- New test in `test_skill_ab_harness.py`:
  `test_inconclusive_verdict_does_not_assert_settled_headline` (above) — the exact
  regression: single task, primary CI excludes 0, verdict `inconclusive`, headline
  must carry the suggestive marker and the honest pill text.
- Structural pattern to follow: `test_build_html_report_renders_and_escapes`
  (`test_skill_ab_harness.py:426`) for the `build_html_report` call + doc-substring
  assertions; `test_single_task_badge_warning` (`:714`) for the single-task setup.
- No existing test should change behavior; all must still pass.
- Verification: `python3 test_skill_ab_harness.py` → `54 passed`.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `python3 test_skill_ab_harness.py` exits 0 and prints `54 passed` (was 53);
      `ok  test_inconclusive_verdict_does_not_assert_settled_headline` appears.
- [ ] `uvx ruff check skill_ab_harness.py` prints `All checks passed!` (exit 0).
- [ ] `uvx ruff check test_skill_ab_harness.py` is clean (no new E402/F401 from the
      added imports).
- [ ] `grep -n "Suggestive only" skill_ab_harness.py` returns exactly one match
      (inside `_HTML_SCRIPT`'s `hero()`).
- [ ] `grep -n "&& settled" skill_ab_harness.py` (or your chosen guard expression)
      shows the assertive headline branch is now gated on the verdict label.
- [ ] No line you added to `_HTML_SCRIPT` exceeds 99 columns
      (`awk 'length>99{print FILENAME":"NR": "length}' skill_ab_harness.py` prints
      nothing — if it prints pre-existing long lines you did not add, those are out
      of scope; confirm none are in your edited region).
- [ ] Only `skill_ab_harness.py` and `test_skill_ab_harness.py` were modified.
- [ ] No new third-party import anywhere
      (`grep -nE "^import (numpy|pandas|pytest|jinja)" *.py` returns nothing).

## STOP conditions

Stop and report back (do not improvise) if:

- The "Current state" excerpts do not match the live code (the file drifted since
  this plan was written — e.g. `hero()` no longer has the `if (lead){...} else {...}`
  shape, or `leadSignal`/`straddles`/`VERD` were refactored). Do not guess.
- After your Step 1 edit, the single-task test setup does **not** produce a primary
  CI that excludes 0 (`lo > 0` assertion fails) — that means the estimator changed
  and the repro no longer holds; report it rather than weakening the test.
- A verification fails twice after a reasonable fix attempt.
- Making the headline honest appears to require touching `badge_verdict`,
  `leadSignal`, or the estimator (out of scope) — it should not.
- You cannot keep an edited JS line under 100 columns without contorting the code —
  report and propose a wrapping rather than shipping a long line.

## Maintenance notes

For whoever owns this next:
- The honesty contract is now: **the pill (`VERD.label`) is the source of truth for
  whether the report may make a settled claim.** Any future hero/headline copy must
  respect `VERD.label === "inconclusive"`. If a third verdict state is added to
  `badge_verdict`/`_verdict_blob`, revisit the `settled` boolean in `hero()`.
- `leadSignal`/`leadIn` still select on CI-clears-0 only; that is intentional (they
  find the *most decisive* signal to describe). The trustworthiness gate lives in
  the presentation layer (`hero()`), not the selector. Keep it that way unless you
  also rework the badge.
- A reviewer should scrutinize: (a) that the assertive branch body is byte-for-byte
  unchanged (only its condition gained `&& settled`), and (b) that the new
  suggestive copy is escaped for arm names (`esc(...)`).
- Deferred on purpose: rendering-DOM-level testing of the headline (would require a
  JS engine / headless browser, which violates the stdlib-only + no-dependency
  rule). The source-presence + blob-label assertions are the agreed substitute.
- Your status row will be added to `plans/README.md` by the reviewer — do not edit
  that index.
