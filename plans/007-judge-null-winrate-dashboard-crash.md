# Plan 007: Guard the blind-judge win-rate so an all-ties / all-failed pair can't blank the HTML dashboard

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. When done, your status row in `plans/README.md`
> will be added by the reviewer who dispatched you — do NOT edit `plans/README.md`
> yourself.
>
> **Drift check (run first)**: this repo is NOT git-initialized, so there is no
> SHA to diff against. Instead, open `skill_ab_harness.py` and compare the
> "Current state" excerpts below (each tagged with `file:line`) to the live code
> before editing. If the code at those locations no longer matches, treat it as a
> STOP condition.

## Status

- **Priority**: P1
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: bug
- **Planned at**: not git-initialized, 2026-06-25
- **Issue**: —

## Why this matters

The interactive HTML report is the primary human-facing deliverable of a run.
When the blind judge produces a comparison pair in which **every** judged verdict
is a tie (or every verdict is a failed/unparseable response), that pair's
`win_rate_a` becomes `NaN` in Python and is serialized to JSON `null`. The
embedded browser JS then calls `j.win_rate_a.toFixed(2)` on `null`, which throws
a `TypeError`. Because the whole interactive page is mounted in a **single**
`el("#app").innerHTML = ...` expression with no `try/catch`, that throw blanks the
ENTIRE interactive report — verdict hero, arm cards, metric charts, audit tables —
leaving only the server-rendered work-product section. An all-ties pair is a
perfectly normal, honest outcome (the judge genuinely couldn't separate two
diffs), so a routine result silently destroys the report. This plan makes
`judgeSection()` null-safe and adds a regression test that locks in the crash
precondition and the guard.

## Current state

Single module under change: `/Users/copyjosh/Code/skills-test/skill_ab_harness.py`
(~3715 lines). The HTML report's JS lives INSIDE a Python raw triple-quoted string
literal `_HTML_SCRIPT` (ES5 vanilla JS — `var`/`function`, no template literals, no
backticks; lines stay under 100 columns). Tests: `/Users/copyjosh/Code/skills-test/test_skill_ab_harness.py`.

**1. The NaN originates in `aggregate_judge` — `skill_ab_harness.py:1552-1555`:**

```python
        out[pkey] = {"a_label": la, "b_label": lb, "a_wins": a_wins, "b_wins": b_wins,
                     "ties": ties, "failed": failed, "decisive": decisive,
                     "win_rate_a": (a_wins / decisive if decisive else float("nan")),
                     "consistent": consistent, "total_pairs": total}
```

`decisive = a_wins + b_wins` (line 1541). When every judged comparison is a tie
(`winner_arm == "tie"`) or a failure (`winner_arm is None`), `a_wins == b_wins == 0`,
so `decisive == 0` and `win_rate_a` is `float("nan")`. This is correct/honest and
should NOT change.

**2. The NaN becomes JSON `null` in `_chart_data` — `skill_ab_harness.py:2996-3005`:**

```python
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
```

`(wr if wr == wr else None)` is the NaN-check (`NaN != NaN`), so `win_rate_a`
lands in the `window.SKILL_AB` blob as `null`. This is also correct and should NOT
change. The blob is serialized at `skill_ab_harness.py:3124`:
`blob = json.dumps(data).replace("</", "<\\/")` (default separators, so the text
`"win_rate_a": null` appears literally in the doc).

**3. The crash is in the embedded JS `judgeSection()` — `skill_ab_harness.py:2778-2831`.**
Four UNGUARDED uses of `j.win_rate_a` (`null` flows straight through):

```javascript
  function judgeSection(){
    var J = D.judge || [];
    if (!J.length) return "";
    var W = 440, padL = 8, padR = 8, pw = W - padL - padR;
    function jx(v){ return padL + v * pw; }
    var anyLow = J.some(function(j){ return j.consistency <= 50; });
    var rows = J.map(function(j){
      var low = j.consistency <= 50, favA = j.win_rate_a >= 0.5;
      var c = favA ? color(j.a) : color(j.b);
      var xc = jx(0.5), xv = jx(j.win_rate_a);
```

(line 2785 `favA = j.win_rate_a >= 0.5`; line 2787 `xv = jx(j.win_rate_a)`)

```javascript
      var t = tip(esc(j.a) + " vs " + esc(j.b), [
        ["win-rate (A):", j.win_rate_a.toFixed(2)],
```

(line 2795 — `.toFixed(2)` on `null` THROWS)

```javascript
        + "<div class='wr num'>" + j.win_rate_a.toFixed(2) + " <span style='"
        + "font-size:11px;font-weight:500;color:var(--faint)'>win-rate A</span></div>"
```

(line 2826 — `.toFixed(2)` on `null` THROWS)

**4. No `try/catch` around mount — `skill_ab_harness.py:2931-2938`:**

```javascript
  function mount(){
    el("#app").innerHTML = header() + hero() + armsSection() + toolbar()
      + judgeSection() + detailsSection();
    renderCharts(defMetric);
    var sel = el("#metric");
    if (sel){ sel.value = defMetric;
      sel.addEventListener("change", function(){ renderCharts(sel.value); }); }
  }
```

The whole page is one `innerHTML` assignment; a throw from `judgeSection()`
aborts the assignment and `#app` stays empty.

**5. Reference data shapes (for the test):**

- `JudgeComparison` dataclass — `skill_ab_harness.py:1351-1360`. Fields:
  `task_id`, `pair_id`, `ordering` (`"a_first"`|`"b_first"`), `winner_arm`
  (winning arm LABEL, `"tie"`, or `None`), `reason`, `pair` (e.g.
  `"my-skill_vs_control"`), `a_label`, `b_label`.
- `build_html_report` signature — `skill_ab_harness.py:3077-3079`:

  ```python
  def build_html_report(results: list[RunResult], pf: Preflight, cfg: ExperimentConfig,
                        manifest: dict, comparisons: list | None = None,
                        seed: int = 0) -> str:
  ```

  Passing a non-empty `comparisons` list is what populates `D.judge` and exercises
  `judgeSection()`.

**6. Test conventions** (`/Users/copyjosh/Code/skills-test/test_skill_ab_harness.py`):

- Custom stdlib runner (NOT pytest); each top-level `def test_*` is collected and
  run. Import alias is `import skill_ab_harness as h`.
- Existing HTML test to MODEL AFTER — `test_skill_ab_harness.py:426-433`:

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

- `_two_task_results()` helper — `test_skill_ab_harness.py:334-341` — returns a
  2-task / 2-arm `list[RunResult]` with `tests_pass` scores and diffs.
- `_cmp(...)` helper for building `JudgeComparison`s — `test_skill_ab_harness.py:240-243`:

  ```python
  def _cmp(task, pid, ordering, winner, a="my-skill", b="control",
           pair="my-skill_vs_control"):
      return h.JudgeComparison(task_id=task, pair_id=pid, ordering=ordering,
                               winner_arm=winner, pair=pair, a_label=a, b_label=b)
  ```

  Use `_cmp` to build an ALL-TIES pair: pass `winner="tie"` for both orderings.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Tests   | `python3 test_skill_ab_harness.py` | all pass (53 existing + your new test), exit 0, <1s, no claude/git/network |
| Lint    | `uvx ruff check skill_ab_harness.py` | `All checks passed!`, exit 0 |
| Lint test file | `uvx ruff check test_skill_ab_harness.py` | `All checks passed!`, exit 0 |

Run all commands from the repo root `/Users/copyjosh/Code/skills-test`.

## Scope

**In scope** (the only files you may modify):
- `/Users/copyjosh/Code/skills-test/skill_ab_harness.py` — the `judgeSection()` JS
  inside `_HTML_SCRIPT` only (the region at lines 2784-2831).
- `/Users/copyjosh/Code/skills-test/test_skill_ab_harness.py` — add ONE new test.

**Out of scope** (do NOT touch, even though they look related):
- `aggregate_judge` (`skill_ab_harness.py:1552-1555`) — the `float("nan")` is the
  correct honest value for "no decisive verdicts"; changing it would hide the
  signal and could break `build_judge_report`'s `wr == wr` text branch.
- `_chart_data`'s `(wr if wr == wr else None)` (line 3003) — `null` is the correct
  JSON encoding of NaN and the regression test asserts on it. Leave it.
- `build_judge_report` (the Markdown report, lines 1559+) — it already guards with
  `wr_s = f"{wr:.3f}" if wr == wr else "n/a"`; not the crash path.
- Any `plans/*.md` file including `plans/README.md` — the reviewer maintains the index.
- STDLIB-ONLY is a hard rule: do NOT add any import or dependency (no numpy /
  pandas / jinja / pytest). Python >= 3.11.

## Git workflow

This repo is NOT git-initialized — there is no branch to create and no commit to
make. Edit the two in-scope files in place. Do not run `git init`.

## Steps

### Step 1: Make `judgeSection()` null-safe in the embedded JS

In `skill_ab_harness.py`, inside the `J.map(function(j){ ... })` callback in
`judgeSection()` (starts at line 2784), introduce a single normalized win-rate and
a display string at the TOP of the callback, then route every use of
`j.win_rate_a` through them. Keep ES5 style (`var`/`function`, no template
literals, no backticks) and keep every line under 100 columns.

Target shape — add these three lines as the FIRST statements inside the map
callback (before `var low = ...`):

```javascript
      var ndec = j.win_rate_a == null;          // no decisive verdicts (all ties/failed)
      var wr = ndec ? 0.5 : j.win_rate_a;       // neutral center when undecidable
      var wrStr = ndec ? "—" : wr.toFixed(2);  // em dash placeholder
```

(`"—"` is an em dash; you may also write the literal `—` — the file is UTF-8
and already contains literals like `≤`/`≈` inside `_HTML_SCRIPT`. The escape form
is safest for column counting.)

Then make these four substitutions in the same callback:

1. Line 2785 — change `favA = j.win_rate_a >= 0.5` to `favA = wr >= 0.5`.
2. Line 2787 — change `xv = jx(j.win_rate_a)` to `xv = jx(wr)`.
3. Line 2795 — change `["win-rate (A):", j.win_rate_a.toFixed(2)],` to
   `["win-rate (A):", wrStr],`.
4. Line 2826 — change `+ "<div class='wr num'>" + j.win_rate_a.toFixed(2) + " <span style='"`
   to `+ "<div class='wr num'>" + wrStr + " <span style='"`.

With `ndec` true, `wr == 0.5` makes the bar collapse to the neutral center
(`xv == xc`, bar width clamped to the existing `Math.max(2, bw)` sliver) and
`wrStr` renders an em dash instead of throwing. Optional (nice-to-have, only if it
keeps lines < 100 cols): when `ndec`, swap the `win-rate A` label text for a short
`no decisive verdicts` note. Not required for correctness.

Do NOT change `aggregate_judge` or `_chart_data`.

**Verify**:
- `grep -n "win_rate_a.toFixed" skill_ab_harness.py` → no matches (both unguarded
  `.toFixed` sites are gone).
- `grep -n "wrStr" skill_ab_harness.py` → at least the definition plus the two
  display uses.
- `uvx ruff check skill_ab_harness.py` → `All checks passed!`

### Step 2: Add a regression test for the all-ties pair

Tests cannot execute the embedded JS, so assert on (a) the Python-produced JSON
blob containing the crash precondition (`win_rate_a` is `null`), and (b) the
presence of the guard token (`wrStr`) in the rendered doc, plus (c) the page
shell still renders. Add ONE test to `test_skill_ab_harness.py`, near
`test_build_html_report_renders_and_escapes` (after line 433). Model it on that
test and use the existing `_cfg()`, `_two_task_results()`, and `_cmp()` helpers.

Target shape:

```python
def test_build_html_report_survives_all_ties_judge_pair():
    # An all-ties pair makes win_rate_a NaN -> JSON null; the embedded JS used to
    # crash on null.toFixed(), blanking the whole #app. The doc must still carry
    # the page shell, the null precondition, and the guard token.
    cfg = _cfg()
    res = _two_task_results()
    man = h.experiment_manifest(cfg, timestamp=1.0)
    comps = [_cmp("A", 0, "a_first", "tie"), _cmp("A", 0, "b_first", "tie")]
    doc = h.build_html_report(res, h.Preflight(), cfg, man, comparisons=comps)
    assert doc.lstrip().startswith("<") and "</html>" in doc
    assert '"win_rate_a": null' in doc            # the JS-crash precondition
    assert "wrStr" in doc                          # the guard landed
```

Notes:
- `winner_arm="tie"` in both orderings gives `a_wins == b_wins == 0`, so
  `decisive == 0` and `win_rate_a` is NaN → serialized `null`. The default
  `json.dumps` separators emit `"win_rate_a": null` (with the space). If the exact
  substring assertion fails, confirm the separator by printing the slice of `doc`
  around `win_rate_a` — do NOT loosen the assertion blindly.
- The `"wrStr" in doc` assertion is the part that FAILS before Step 1 and PASSES
  after — it is the real regression anchor.

**Verify**: `python3 test_skill_ab_harness.py` → all pass including the new test.

## Test plan

- New test in `test_skill_ab_harness.py`:
  `test_build_html_report_survives_all_ties_judge_pair` — covers the regression:
  an all-ties judge pair produces `"win_rate_a": null` in the blob and the doc
  still contains the page shell and the `wrStr` guard token.
- Structural pattern to copy: `test_build_html_report_renders_and_escapes`
  (`test_skill_ab_harness.py:426`), reusing helpers `_cfg`, `_two_task_results`,
  `_cmp`.
- The happy path (a decisive judge pair) is already covered by existing judge
  tests (`test_aggregate_consistent_preference`,
  `test_build_judge_report_empty_and_basic`); no new happy-path test needed.
- Verification: `python3 test_skill_ab_harness.py` → all pass (existing 53 + 1 new).

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `python3 test_skill_ab_harness.py` exits 0; the new test
      `test_build_html_report_survives_all_ties_judge_pair` exists and passes.
- [ ] `uvx ruff check skill_ab_harness.py` prints `All checks passed!` (exit 0).
- [ ] `uvx ruff check test_skill_ab_harness.py` prints `All checks passed!` (exit 0).
- [ ] `grep -n "win_rate_a.toFixed" skill_ab_harness.py` returns no matches.
- [ ] `grep -n "wrStr" skill_ab_harness.py` returns at least one match.
- [ ] No files outside the in-scope list are modified.
- [ ] No new imports/dependencies added (stdlib-only preserved).
- [ ] `plans/README.md` row added by the reviewer (you did NOT edit it).

## STOP conditions

Stop and report back (do not improvise) if:

- The code at the cited lines in "Current state" does not match the excerpts
  (the module drifted since this plan was written — recall there is no SHA, so
  the excerpts are the only baseline).
- After the Step 1 edits, `'"win_rate_a": null' in doc` is False — that means the
  NaN→null path changed; investigate `aggregate_judge`/`_chart_data` and report
  rather than weakening the test.
- `uvx ruff` flags an E501 (line ≥ 100 cols) you cannot resolve without
  restructuring beyond the four substitutions + three added lines.
- A step's verification fails twice after a reasonable fix attempt.
- The fix appears to require editing `aggregate_judge`, `_chart_data`, or any
  file outside the in-scope list.

## Maintenance notes

For whoever owns this code next:
- If the embedded report is ever migrated to render `judgeSection()` via a path
  that DOES throw outside the single `innerHTML` assignment (e.g. per-row
  rendering), revisit whether `mount()` (`skill_ab_harness.py:2931`) should also
  wrap the assignment in a `try/catch` as defense-in-depth. This plan deliberately
  fixes the data-handling bug at the source (`judgeSection`) rather than masking
  it with a catch-all.
- The em dash placeholder (`—`) matches existing non-ASCII usage inside
  `_HTML_SCRIPT`; if the JS is ever transpiled or minified, confirm the encoding
  survives.
- Reviewer should scrutinize: that all four `j.win_rate_a` use-sites were rerouted
  through `wr`/`wrStr` (a missed one re-introduces the crash), and that the
  neutral-center bar (`ndec` → `wr = 0.5`) does not visually imply a real 0.50
  win-rate — ideally the em dash label makes "undecidable" unambiguous.
- Follow-up deliberately deferred: applying the same neutral treatment to the
  low-consistency hatch styling for `ndec` pairs (cosmetic; out of scope here).
