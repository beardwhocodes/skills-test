# Plan 013: Close the chart-tooltip XSS where attribute→innerHTML round-trip undoes escaping

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. A reviewer maintains `plans/README.md`; do NOT
> edit the index yourself (your row is added separately).
>
> **Drift check (run first)**: This repo is NOT git-initialized, so there is no
> SHA to diff against. Before editing, compare the "Current state" excerpts
> below against the live code in `skills_test.py` at the cited line
> numbers. If the live code differs from any excerpt, treat it as a STOP
> condition (the file has drifted since this plan was written).

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: security
- **Planned at**: not git-initialized, 2026-06-25

## Why this matters

The HTML report is a single self-contained file that gets shared (committed to
CI artifacts, emailed, pasted into chat). Its interactive SVG charts show
tooltips whose text includes **arm labels** — and arm labels are derived from
attacker-influenceable strings: `skill_name`, the `skills-test quick A B` CLI
arguments, and a `--from-github` config. The tooltip code escapes that text
once, but then stores it in a `data-tip="..."` attribute and later does
`tip.innerHTML = node.getAttribute("data-tip")`. `getAttribute` returns the
**entity-decoded** attribute value, so a single-escaped `&lt;img&gt;` decodes
back to `<img>` and `innerHTML` re-parses it as **live HTML**. A skill or arm
label like `<img src=x onerror=alert(1)>` therefore executes JavaScript in
whoever opens the shared report. This plan makes the tooltip text survive that
one attribute-decode as inert text, closing the injection.

The JSON data blob (`window.SKILLS_TEST`) is a **separate, already-safe** path: it
is emitted inside a `<script>` element with `json.dumps(...).replace("</",
"<\\/")` (see `build_html_report`, line 3124), so raw `<img` sitting in a JSON
string there is inert text, not markup. This plan does **not** touch that path.

## Current state

Single module: `skills_test.py` (~3715 lines). The report's client-side JS
lives inside a Python raw triple-quoted string literal `_HTML_SCRIPT` (starts at
line 2201, `_HTML_SCRIPT = r"""(function(){`). It is **ES5 vanilla JS**:
`var`/`function` only, no `let`/`const`/arrow/template-literals/backticks, lines
stay under 100 columns. The CSS lives in `_HTML_STYLE` (line 1857). Comments
explain *why*, not *what*.

### The escaping helper (line 2215) — single HTML-escape, used everywhere

```javascript
  function esc(s){
    return String(s).replace(/[&<>"]/g, function(c){
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];
    });
  }
```

### The vulnerable tooltip builder (lines 2288–2294)

```javascript
  function tip(head, rows){
    var h = "<div class='tt-h'>" + esc(head) + "</div>";
    var body = rows.map(function(r){
      return "<div class='tt-r'>" + r[0] + " <b>" + r[1] + "</b></div>";
    }).join("");
    return (h + body).replace(/"/g, "&quot;");
  }
```

Two defects:
- `head` is escaped only **once** (`esc(head)`) — undone by the attribute decode.
- The row cells `r[0]` and `r[1]` are interpolated with **no escaping at all**.

### The four call sites that emit `data-tip` from `tip(...)`

All four are JS (built in the browser from the blob), not server-rendered.
`MINUS` and `MIDDOT` are JS constants defined at line 2207
(`var MINUS = "−", MIDDOT = "·";`) — plain Unicode, not in the `[&<>"]` escape
set.

`barChart` (lines 2624, 2629) — head passed **raw**:
```javascript
      var t = tip(a, [
        [metricLabel(m) + ":", fmtVal(m, v, m === "cost_usd" ? 4 : 1)],
        ["valid runs:", validCount(a) + " / " + runsFor(a).length],
        ["direction:", dirOf(m) < 0 ? "lower is better" : "higher is better"]
      ]);
      return "<g class='hit' data-tip=\"" + t + "\">"
```

`forestChart` (lines 2682–2691) — head and one row cell are **pre-`esc()`'d**:
```javascript
      var t = tip(esc(c.a) + " " + MINUS + " " + esc(c.b), [
        ["Δ " + metricLabel(m) + ":", fmtSigned(m, d.point, m==="cost_usd"?4:1)],
        ["95% CI:", "[" + fmtVal(m, d.lo, m==="cost_usd"?3:1) + ", "
          + fmtVal(m, d.hi, m==="cost_usd"?3:1) + "]"],
        ["p / q:", (d.p == null ? "—" : d.p.toFixed(3)) + " / "
          + (d.q == null ? "—" : d.q.toFixed(3))],
        ["verdict:", ns ? "CI crosses 0 (n.s.)"
          : (q === "good" ? "favors " + esc(c.a) : "favors " + esc(c.b))]
      ]);
      return "<g class='hit' data-tip=\"" + t + "\">"
```

`stripChart` (lines 2745, 2751) — head passed **raw**:
```javascript
        var t = tip(a + " " + MIDDOT + " run #" + r.idx, [
          [metricLabel(m) + ":", fmtVal(m, r.scores[m], m==="cost_usd"?4:1)],
          ["cost:", "$" + (r.cost == null ? "?" : r.cost.toFixed(4))],
          ["turns:", String(r.turns == null ? "?" : r.turns)],
          ["valid:", r.valid ? "yes" : "no"]
        ]);
        return "<circle class='hit' data-tip=\"" + t + "\" cx='" + jx.toFixed(1)
```

`judgeSection` (lines 2794, 2807) — head **pre-`esc()`'d**:
```javascript
      var t = tip(esc(j.a) + " vs " + esc(j.b), [
        ["win-rate (A):", j.win_rate_a.toFixed(2)],
        ["record:", j.a_wins + "–" + j.b_wins + "–" + j.ties
          + " (A–B–tie)"],
        ["consistency:", j.consistency + "%" + (low ? " (≈ chance)" : "")]
      ]);
```

So `head` is single-escaped (raw at 2624/2745, pre-`esc()`'d at 2682/2794) and
row cells are unescaped — both are defeated by the round-trip.

### The round-trip that decodes the escaping (lines 2939–2944)

```javascript
  function wireTooltip(){
    var t = el("#tip");
    document.addEventListener("mouseover", function(e){
      var n = e.target.closest ? e.target.closest("[data-tip]") : null;
      if (n){ t.innerHTML = n.getAttribute("data-tip"); t.classList.add("on"); }
    });
```

`getAttribute` entity-decodes once, then `innerHTML` parses the result as HTML —
two decode stages total.

### Why the fix is "escape twice"

There are exactly **two** decode stages on the way to rendered output
(`getAttribute` decode, then `innerHTML` HTML-parse). To make a dynamic string
render as its correct literal text AND stay inert, escape it exactly **twice**.
Walk it through for `<img src=x onerror=alert(1)>`:

- `esc(esc(s))` = `&amp;lt;img src=x onerror=alert(1)&amp;gt;`
- attribute source: `data-tip="...&amp;lt;img...&amp;gt;..."`
- `getAttribute` decodes `&amp;`→`&`, yielding `&lt;img...&gt;`
- `innerHTML` parses `&lt;img...&gt;` as the **text** `<img...>` — no element, no
  `onerror`, no execution.

The same double-escape round-trips legitimate content correctly: a label
`foo & bar` becomes `foo &amp;amp; bar` → decode → `foo &amp; bar` → innerHTML →
renders `foo & bar`. Plain labels with no `&<>"` characters (e.g. `my-skill`)
are unchanged by either `esc` pass, so normal tooltips look identical. The
static `<div class='tt-h'>` / `<b>` wrappers stay literal (escaped zero times)
so the tooltip keeps its structure.

## Commands you will need

| Purpose | Command | Expected on success |
|---------|---------|---------------------|
| Tests   | `python3 test_skills_test.py` | all tests pass (currently 53), exit 0, < 1s |
| Lint    | `uvx ruff check skills_test.py` | `All checks passed!`, exit 0 |
| Line-length spot check | `awk 'length>100{print FILENAME":"NR": "length}' skills_test.py` | no output |

Run from the repo root `/Users/copyjosh/Code/skills-test`. The test runner is a
custom stdlib runner (NOT pytest); it needs no `claude`, `git`, or network.

## Scope

**In scope** (the only files you should modify):
- `skills_test.py` — only the `tip()` function and the four `tip(...)`
  call expressions inside `_HTML_SCRIPT` (and one new tiny helper next to it).
- `test_skills_test.py` — add one new test.

**Out of scope** (do NOT touch, even though they look related):
- The JSON blob path: `json.dumps(data).replace("</", "<\\/")` at line 3124 and
  anything in `_chart_data`. It is already safe (inert inside `<script>`);
  changing it is unnecessary and risks breaking the charts.
- Every **other** `esc(...)` call in the file — in particular the SVG `<text>`
  nodes such as `esc(clip(c.a, 19))` at lines 2695/2697 and `esc(cheapest)` at
  line 2487. Those render directly via `innerHTML` (NOT through the `data-tip`
  attribute round-trip) and correctly need a **single** escape. Do not change
  them. Only remove `esc()` calls that are **arguments to `tip(...)`**.
- `wireTooltip` (lines 2939–2957). The fix is in what gets stored in `data-tip`,
  not in how it is read. Leave the reader alone.
- Any **other** injection path you happen to spot — note it in your report as a
  separate finding; do not fix it here. (The report title is already safe:
  `html.escape(title)` server-side at line 3141 and a single `esc` via direct
  `innerHTML` at line 2383 — both correct for their non-round-trip paths. Don't
  "fix" them.)

## Git workflow

This repo is **not** a git repository. There is no branch to create and no
commit to make — edit the files in place. Do not run `git` commands.

## Steps

### Step 1: Add a double-escape helper and harden `tip()`

In `_HTML_SCRIPT`, replace the `tip()` function (lines 2288–2294) so that the
**head and both row cells** are escaped twice, while the static `<div>`/`<b>`
wrappers stay literal. Add a small `escTip` helper (place it immediately above
`tip`, after `esc` which is defined at line 2215 so it is in scope).

Target shape:

```javascript
  // Tooltip text is stored in a data-tip attribute, then read back via
  // getAttribute (one entity-decode) and assigned to innerHTML (a second
  // HTML-parse). Two decodes => escape dynamic text twice so it survives as
  // inert text instead of re-parsing as live markup (XSS via arm labels).
  function escTip(s){ return esc(esc(s)); }
  function tip(head, rows){
    var h = "<div class='tt-h'>" + escTip(head) + "</div>";
    var body = rows.map(function(r){
      return "<div class='tt-r'>" + escTip(r[0]) + " <b>" + escTip(r[1])
        + "</b></div>";
    }).join("");
    // escTip already neutralizes any '"'; the trailing replace is a defensive
    // backstop for the attribute delimiter.
    return (h + body).replace(/"/g, "&quot;");
  }
```

Keep ES5 (no arrow functions, no template literals) and every line under 100
columns.

**Verify**: `python3 test_skills_test.py` → all existing tests still pass
(exit 0). `uvx ruff check skills_test.py` → `All checks passed!`.

### Step 2: Remove the now-redundant `esc()` calls inside the `tip(...)` arguments

Because `tip()` now escapes its inputs (twice), any caller that pre-`esc()`'s a
value passed **into `tip(...)`** would over-escape and show literal `&lt;`
artifacts in normal tooltips. Make all four call sites pass **raw** label
values. Change ONLY the `esc(...)` calls that are arguments to `tip(...)`:

- `forestChart`, line 2682 — head:
  `tip(esc(c.a) + " " + MINUS + " " + esc(c.b), [`
  → `tip(c.a + " " + MINUS + " " + c.b, [`
- `forestChart`, line 2689 — verdict row:
  `: (q === "good" ? "favors " + esc(c.a) : "favors " + esc(c.b))]`
  → `: (q === "good" ? "favors " + c.a : "favors " + c.b)]`
- `judgeSection`, line 2794 — head:
  `tip(esc(j.a) + " vs " + esc(j.b), [`
  → `tip(j.a + " vs " + j.b, [`

`barChart` (line 2624, `tip(a, ...)`) and `stripChart` (line 2745,
`tip(a + " " + MIDDOT + ...)`) already pass raw heads — leave them as they are.

Do **not** remove the `esc(...)` calls elsewhere in these same functions (the
SVG `<text>` nodes at lines 2695/2697 etc.) — those are out of scope and still
need their single escape.

**Verify**: `grep -n "tip(" skills_test.py` shows no `esc(` inside any
`tip(...)` argument list. `python3 test_skills_test.py` → all pass.
`uvx ruff check skills_test.py` → clean.

### Step 3: Add the regression test

Add a test to `test_skills_test.py` (model it after the existing
`test_build_html_report_renders_and_escapes` at line 426, which uses
`_two_task_results()` from line 334, `_cfg()` from line 18,
`h.experiment_manifest(...)`, and `h.Preflight()`).

The tooltip `data-tip` attributes are built **client-side** by the embedded JS,
so they do not appear as literal attributes in the generated `doc` string — a
stdlib-only Python test cannot execute the JS to inspect the rendered DOM.
Therefore assert on the **generated JS source** that the hardening is present
and the vulnerable patterns are gone. Also smoke-test that a malicious
`skill_name` does not crash report generation.

IMPORTANT — do NOT assert `"<img" not in doc`. The malicious string legitimately
appears as **inert** text inside the `window.SKILLS_TEST` JSON blob (a JSON string
inside a `<script>`, protected by the `</`→`<\/` escaping at line 3124). That is
a different, already-safe path; asserting its absence would fail even after the
fix is correct.

Target test:

```python
def test_tooltip_payload_is_double_escaped_against_attr_roundtrip():
    # Arm labels come from skill_name / CLI args / --from-github config and are
    # rendered into data-tip="..." then read back via getAttribute (one decode)
    # into innerHTML (a second decode). The tooltip builder must escape dynamic
    # text TWICE so a markup label can't re-parse as live HTML (XSS).
    cfg = _cfg(skill_name="<img src=x onerror=alert(1)>")
    res = _two_task_results()
    man = h.experiment_manifest(cfg, timestamp=1.0)
    doc = h.build_html_report(res, h.Preflight(), cfg, man)  # must not raise
    assert doc.lstrip().startswith("<") and "</html>" in doc

    # The hardened tooltip builder is present: a double-escape helper applied to
    # the head and BOTH row cells, and the old raw row interpolation is gone.
    assert "function escTip(s){ return esc(esc(s)); }" in doc
    assert "escTip(head)" in doc
    assert "escTip(r[0])" in doc and "escTip(r[1])" in doc
    # the pre-fix vulnerable shapes must not survive:
    assert '"<div class=\'tt-h\'>" + esc(head) + "</div>"' not in doc
    assert '" <b>" + r[1] + "</b>"' not in doc
```

**Verify**: `python3 test_skills_test.py` → all pass, including the new
test (count goes from 53 to 54).

## Test plan

- New test in `test_skills_test.py`:
  `test_tooltip_payload_is_double_escaped_against_attr_roundtrip` — covers the
  happy path (report builds with a markup `skill_name` without raising) and the
  regression (the JS tooltip builder double-escapes head + both row cells; the
  pre-fix single-escape head and raw `r[1]` interpolation are absent).
- Structural model: `test_build_html_report_renders_and_escapes` (line 426).
- Why source-level, not DOM-level: stdlib-only is a hard rule (no JS engine, no
  new dependency). The fix lives in `_HTML_SCRIPT` (client-side JS), so the
  faithful, dependency-free guard is to assert the hardened source is emitted
  and the vulnerable source is not. The two-decode→two-escape reasoning is
  documented in "Current state" above for the reviewer.
- Verification: `python3 test_skills_test.py` → all pass (54 tests);
  `uvx ruff check skills_test.py` → clean.

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `python3 test_skills_test.py` exits 0; the new test
  `test_tooltip_payload_is_double_escaped_against_attr_roundtrip` exists and
  passes (total test count 54).
- [ ] `uvx ruff check skills_test.py` prints `All checks passed!` (exit 0).
- [ ] `awk 'length>100{print NR}' skills_test.py` prints nothing (no line
  exceeds 100 columns).
- [ ] `grep -n "function escTip" skills_test.py` returns exactly one match.
- [ ] No `esc(` remains inside any `tip(...)` argument list (manual check of the
  four call sites at lines ~2624/2682/2745/2794).
- [ ] Only `skills_test.py` and `test_skills_test.py` were modified.

## STOP conditions

Stop and report back (do not improvise) if:

- The live code at the cited lines does not match the "Current state" excerpts
  (the file has drifted since this plan was written) — especially if `tip()`,
  `esc()`, `wireTooltip`, or any of the four call sites already look different.
- A verification command fails twice after a reasonable fix attempt.
- Removing an `esc()` at a call site appears to also affect an SVG `<text>` node
  (out of scope) — i.e., the `esc()` you'd remove is NOT actually an argument to
  `tip(...)`. Leave it and report.
- You discover a genuinely **new** injection path (one that renders dynamic text
  as live HTML without escaping) — note it as a separate finding and do not fix
  it here. (The title path is already escaped — do not flag that one.)
- The fix would require adding any dependency (numpy/pandas/jinja/pytest/etc.) —
  stdlib-only is a hard rule. It should not; if it seems to, stop.

## Maintenance notes

- **Any new tooltip caller must go through `tip()`** and pass **raw** values —
  never pre-`esc()` a value handed to `tip()`. `tip()` owns all escaping for the
  `data-tip` round-trip; callers that escape first will over-escape and show
  literal `&lt;` in normal tooltips.
- The double-escape is calibrated to exactly the two decode stages in
  `wireTooltip` (`getAttribute` decode + `innerHTML` parse). If a future change
  alters how tooltip text is stored or read — e.g. moving from `data-tip` +
  `innerHTML` to `textContent`, or to a JS-side payload map keyed by element id
  — the escaping count must be revisited (with `textContent` you would need
  **zero** HTML-escaping; with one decode stage, **one**).
- Reviewer should scrutinize: that no `esc()` was removed from an SVG `<text>`
  node (those still need a single escape), and that the JSON-blob path (line
  3124) was left untouched.
- Deferred out of scope: a structural refactor to stop round-tripping HTML
  through an attribute entirely (store the tooltip payload in a JS object keyed
  by an element id and build the tooltip with `textContent` for dynamic parts).
  That is cleaner long-term but a larger change touching `tip()`, all four call
  sites, and `wireTooltip`; the double-escape here is the smallest correct fix.
- Your status row in `plans/README.md` is added by the reviewer — do not edit
  the index.
