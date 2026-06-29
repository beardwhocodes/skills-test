# Plan 023: Optional total-spend ceiling in the serve UI ("stop starting new runs past ~$N")

> **Executor instructions**: Follow this plan step by step. Run every verification
> command and confirm the expected result before moving to the next step. If anything
> in the "STOP conditions" section occurs, stop and report — do not improvise. When
> done, update the status row for this plan in `plans/README.md`.
>
> **Drift check (run first)**:
> `git diff --stat b991faf..HEAD -- skills_test.py skills_test_server.py skills_test_app.py`
> If any in-scope file changed since this plan was written, compare the "Current state"
> excerpts against the live code before proceeding; on a mismatch, treat it as a STOP
> condition.

## Status

- **Priority**: P2
- **Effort**: M
- **Risk**: LOW (a numeric guard; no change to the estimand or scoring)
- **Depends on**: plans/021 (serve app) — DONE
- **Category**: dx / safety
- **Planned at**: commit `b991faf`, 2026-06-26

## Why this matters

The engine already has a `cost_ceiling_usd` knob, but the **serve API never sets it**,
so web-launched runs have **no spend brake at all**. A real run proved the cost: a
too-big task ("resolve all review comments on a PR") hit the 30-turn cap on most runs
and spent **$12.34** against an estimate of ~$1–2, with 4 of 6 runs invalid — money
spent on runs that get excluded from the analysis. The estimate is only a projection;
nothing enforces it.

This plan adds an **opt-in** ceiling to the New-run form so a user can bound total
usage. The semantics are deliberately honest and must be messaged as such: the ceiling
**declines to *start* new runs** once cumulative spend crosses ~$N; it **does NOT kill
in-flight runs** (killing paid work mid-flight wastes it). So actual usage can exceed N
by up to one wave of already-running runs — it's a soft cap on a usage *proxy*, not a
hard dollar limit. It is **off by default** (empty field → no ceiling, today's
behavior).

## Current state

Files and their roles:

- `skills_test.py` — the engine. Already has the ceiling machinery; this plan adds
  ONE event emit so a skipped run isn't invisible to the UI.
- `skills_test_server.py` — `_build_run_config` builds the `ExperimentConfig` from a
  request but never sets `cost_ceiling_usd`.
- `skills_test_app.py` — the SPA. The New-run form (`viewNew`) and the live view
  (`viewLive`). The usage ticker already renders a ceiling; the form doesn't collect one.

### Engine — the ceiling already works, except for one UX gap

`skills_test.py`, the config field (≈ line 211):
```python
    cost_ceiling_usd: float | None = None   # abort remaining runs once total cost crosses this
```

The stop predicate (search `def _should_stop`):
```python
def _should_stop(spent: float, ceiling: float | None) -> bool:
    return ceiling is not None and spent >= ceiling
```

`run_experiment` accumulates spend and flips the stop flag after each run lands
(≈ line 1328-1331):
```python
                budget["spent"] += res.cost_usd or 0.0
                if _should_stop(budget["spent"], cfg.cost_ceiling_usd):
                    budget["stop"] = True
```

**The gap**: the budget gate in `execute_run` sits INSIDE the semaphore but BEFORE the
`run_start` event is emitted (≈ line 1068-1074), so a skipped run emits **nothing** —
no `run_start`, no `run_done`:
```python
    async with sem:
        # Budget gate INSIDE the semaphore: queued runs see stop only after they
        # acquire a slot, so the ceiling actually bounds spend ...
        if budget is not None and budget["stop"]:
            return _failed_result(task, arm, idx, cfg, "budget ceiling reached", _SKIPPED_ERR)
        _emit(on_event, {"type": "run_start", "label": label})
```
And `run_and_persist` returns early on `_SKIPPED_ERR` without emitting `run_done`
(≈ line 1322-1324):
```python
        res = await execute_run(task, arm, i, cfg, scorers, pf, sem, budget, on_event)
        if res.error == _SKIPPED_ERR:         # ceiling skip -> don't persist or count
            return res
```
Result: in the live grid, a ceiling-skipped cell created at `experiment_start` stays
stuck on **"pending"** forever. Step 2 fixes this with a `run_skipped` event.

### Server — where the ceiling must be plumbed in

`skills_test_server.py`, `_build_run_config` builds the cfg (≈ line 305-315). It already
shows the numeric-validation/clamp style with the `k` field:
```python
    cfg = h.ExperimentConfig(
        repo_path=repo, base_ref=base_ref,
        skill_src=md_a.parent, skill_name=skill_a,
        skill_b_src=skill_b_src, skill_b_name=skill_b_name, runner_b=runner_b,
        model_a=req.get("model_a") or None,
        # a codex arm has no claude model -> don't attach one (keeps the arm label clean)
        model_b=None if runner_b else (req.get("model_b") or None),
        include_control=bool(req.get("include_control", True)),
        k=max(1, min(20, int(req.get("k") or 3))),   # clamp: UI caps 1-10; never runaway
        judge_enabled=bool(req.get("judge")),
        disallowed_tools=deny, isolation=isolation, results_dir=run_dir)
```
`_build_run_config` raises `ValueError`/`SystemExit` on bad input; `_handle_start`
catches it and returns **400** (see `_handle_start`), so raising is the correct way to
reject a malformed ceiling. **Do NOT** add the ceiling to `_build_estimate_config` — the
ceiling does not change the run count the estimate reports.

### Frontend — form payload and the already-ceiling-aware ticker

`skills_test_app.py`, the request builder `req(demo)` (search `function req(`) returns the
POST body; add `cost_ceiling_usd` there. Form inputs use the `E("input", {class:"inp",
...})` helper and the `field(label, hint, control)` wrapper (search `function field(`).
Every interactive field is registered for `invalidate` in the
`[skillA, skillB, ...].forEach(...)` block (search `.forEach(function(n){`).

The live usage ticker **already supports a ceiling** (search `function setTicker`):
```js
  function setTicker(spent, ceiling){
    tickerEl.hidden = false;
    ... (ceiling != null ? " / " + Number(ceiling).toFixed(2) : "");
    var pct = ceiling ? Math.min(100, (s / ceiling) * 100) : 0;
```
It is driven by the live view's `cost` event handler, and the engine's `cost` event
already carries `ceiling_usd` (it reads `cfg.cost_ceiling_usd`). So **once the cfg has a
ceiling, the live ticker shows it automatically** — no ticker change needed. You only
need to (a) collect the value in the form, (b) handle the new `run_skipped` event.

## Commands you will need

| Purpose            | Command                                                        | Expected on success            |
|--------------------|---------------------------------------------------------------|--------------------------------|
| Lint               | `uvx ruff check skills_test.py skills_test_server.py skills_test_app.py test_skills_test.py test_skills_test_server.py` | `All checks passed!`           |
| Engine tests       | `python3 test_skills_test.py`                            | `N passed` (N ≥ current + new) |
| Server tests       | `python3 test_skills_test_server.py`                             | `N passed` (N ≥ current + new) |
| App JS syntax      | `python3 -c "import skills_test_app as a; open('/tmp/_a.js','w').write(a._APP_JS)" && node --check /tmp/_a.js` | `/tmp/_a.js` OK, exit 0 |

(stdlib-only project; the test runner is custom, NOT pytest. `ruff` line-length is 100.)

## Scope

**In scope** (the only files you should modify):
- `skills_test.py` — Step 2 only (emit `run_skipped`).
- `skills_test_server.py` — Step 1 (parse/validate/pass the ceiling).
- `skills_test_app.py` — Steps 3 & 4 (form field; `run_skipped` cell handling + CSS).
- `test_skills_test_server.py`, `test_skills_test.py` — Step 5.
- `README.md` / `CLAUDE.md` — one-line doc note (Step 6).

**Out of scope** (do NOT touch, even though they look related):
- **Killing in-flight runs.** The ceiling only declines to START new runs. Do not add
  any mid-run abort based on cost — paid work in flight must finish and be scored.
- `_build_estimate_config` — the ceiling does not affect the projected run count.
- Deriving the ceiling automatically from the estimate, or any per-arm ceiling.
- The estimand, scorers, validity rules, or the `cost` event shape.

## Git workflow

- This repo's history uses descriptive multi-line commit subjects (see `git log
  --oneline`). Match that. End the commit body with the two trailers already used in
  this repo's commits (`🤖 Co-Authored-By: Claude` + `Claude-Session: ...`).
- One commit for the whole plan is fine. Do NOT push or open a PR unless instructed.

## Steps

### Step 1: Parse, validate, and pass the ceiling in `_build_run_config`

In `skills_test_server.py`, just before the `cfg = h.ExperimentConfig(...)` call in
`_build_run_config`, add:
```python
    # Optional total-spend ceiling (usage proxy). Off when absent/blank. Positive number
    # only; clamp to a sane max so a typo can't disable the guard. Caps NEW runs, never
    # kills in-flight ones (see plan 023).
    ceiling_raw = req.get("cost_ceiling_usd")
    cost_ceiling = None
    if ceiling_raw not in (None, ""):
        try:
            cost_ceiling = float(ceiling_raw)
        except (TypeError, ValueError):
            raise ValueError("cost_ceiling_usd must be a number")
        if cost_ceiling <= 0:
            raise ValueError("cost_ceiling_usd must be a positive number")
        cost_ceiling = min(cost_ceiling, 1000.0)
```
Then add `cost_ceiling_usd=cost_ceiling,` to the `ExperimentConfig(...)` keyword args
(put it next to `judge_enabled=...`).

**Verify**: `python3 -c "import skills_test_server as s, skills_test as h; from pathlib import Path; h.resolve_skill=lambda n: Path('/x/'+n+'/SKILL.md'); h.resolve_target=lambda t,p: (Path('/r'),'HEAD',p or 'x',None,None); import tempfile; d=tempfile.mkdtemp(); print(s._build_run_config({'skill_a':'a','target':'.','prompt':'x','cost_ceiling_usd':'5'}, Path(d))[0].cost_ceiling_usd)"`
→ prints `5.0`

### Step 2: Emit a `run_skipped` event so ceiling-skipped cells aren't invisible

In `skills_test.py`, `execute_run`, change the budget gate to emit before
returning:
```python
        if budget is not None and budget["stop"]:
            _emit(on_event, {"type": "run_skipped", "label": label,
                             "reason": "cost ceiling reached"})
            return _failed_result(task, arm, idx, cfg, "budget ceiling reached", _SKIPPED_ERR)
```
(Only add the `_emit(...)` line; leave the return unchanged. `_SKIPPED_ERR` results stay
filtered out of persistence and analysis — do not change that.)

**Verify**: `grep -n "run_skipped" skills_test.py` → one match inside `execute_run`.

### Step 3: Add the optional ceiling field to the New-run form

In `skills_test_app.py`, in `viewNew`, create the input near the other config fields (e.g.
beside the `k` slider / isolation row):
```js
    var ceilingEl = E("input", {class:"inp", id:"f-ceiling", name:"cost_ceiling_usd",
      type:"number", min:"0", step:"0.5", inputmode:"decimal", autocomplete:"off",
      placeholder:"off", "aria-label":"stop after total usage"});
```
Add it to the layout with the `field(...)` wrapper and an honest hint, e.g.:
```js
        field("Stop after ~$ total (optional)",
          "soft cap on the usage proxy: stops STARTING new runs past this — " +
          "in-flight runs still finish, so actual usage can exceed it. Blank = off.",
          ceilingEl),
```
Register it for invalidation by adding `ceilingEl` to the existing
`[skillA, skillB, ... ].forEach(...)` array. Then in `req(demo)`, add to the returned
object:
```js
        cost_ceiling_usd: ceilingEl.value.trim() || null,
```

**Verify**: app JS syntax command above → exit 0; and
`python3 -c "import skills_test_app as a; print('f-ceiling' in a._APP_JS and 'cost_ceiling_usd' in a._APP_JS)"`
→ `True`

### Step 4: Mark ceiling-skipped cells in the live view + add CSS

In `skills_test_app.py`, `viewLive`, the event dispatch object has handlers like
`run_done: function(ev){ ... }` (search `run_done: function`). Add a sibling handler:
```js
      run_skipped: function(ev){
        setCell(ev.label, "skipped");
        appendLine(ev.label, "skipped — " + (ev.reason || "cost ceiling reached"), "sys");
      },
```
Add a CSS rule next to `.cell.invalid` (search `.cell.invalid{`):
```css
  .cell.skipped{border-color:var(--line); background:var(--card-2)}
  .cell.skipped .c-st{color:var(--muted)}
```
(If `setCell` validates the status string against a fixed set, add `"skipped"` to that
set — search `function setCell`.)

**Verify**: `python3 -c "import skills_test_app as a; print('run_skipped' in a._APP_JS and 'cell.skipped' in a._APP_JS)"`
→ `True`; app JS `node --check` → exit 0.

### Step 5: Tests

In `test_skills_test_server.py`, add (model after `test_build_run_config_runner_b_preset_...`
which already stubs `h.resolve_skill`/`h.resolve_target`):
```python
def test_build_run_config_parses_and_validates_cost_ceiling():
    orig_skill, orig_target = h.resolve_skill, h.resolve_target
    h.resolve_skill = lambda name: Path(f"/fake/skills/{name}/SKILL.md")
    h.resolve_target = lambda target, prompt: (Path("/fake/repo"), "HEAD",
                                               prompt or "x", None, None)
    try:
        with tempfile.TemporaryDirectory() as td:
            mk = lambda v: s._build_run_config(
                {"skill_a": "a", "target": ".", "prompt": "x", "cost_ceiling_usd": v},
                Path(td))[0]
            assert mk("5").cost_ceiling_usd == 5.0
            assert mk("").cost_ceiling_usd is None        # blank = off
            assert mk(None).cost_ceiling_usd is None
            assert mk("9999").cost_ceiling_usd == 1000.0  # clamped
            for bad in ("-1", "0", "abc"):
                try:
                    mk(bad); assert False, f"should reject {bad!r}"
                except ValueError:
                    pass
    finally:
        h.resolve_skill, h.resolve_target = orig_skill, orig_target
```
In `test_skills_test.py`, add a test that the budget gate emits `run_skipped`. Drive
`execute_run` with a tripped budget and a no-op scorer set, capturing events (model after
the existing `execute_run`/event tests; use a `tempfile` worktree_root and a real tiny
git repo only if an existing test already does — otherwise assert at the unit level that
a pre-tripped `budget={"stop":True}` makes `execute_run` emit a `run_skipped` event and
return a `_SKIPPED_ERR` result). If wiring a full `execute_run` is too heavy without
`git`, instead assert the narrower contract: the event dict shape is emitted — search for
an existing test that calls `execute_run` and follow its setup; if none exists, document
in the test file why this path is covered by the server/E2E layer and assert
`grep`-level that the emit exists is NOT acceptable — prefer a real event capture.

**Verify**: both test commands above → all pass, including the 2 new tests.

### Step 6: One-line doc note

Add a sentence to the `README.md` "Local app" section noting the optional spend ceiling
("set *Stop after ~$N* to cap total usage — stops starting new runs past the threshold;
in-flight runs finish"). Add a matching bullet to `CLAUDE.md` near the serve/plan-022
notes. Keep it to 1–2 lines each.

**Verify**: `grep -in "stop after\|cost ceiling\|spend" README.md` → at least one match.

## Test plan

- **Server** (`test_skills_test_server.py`): the happy path (`"5"` → `5.0`), off-by-default
  (`""`/`None` → `None`), clamp (`"9999"` → `1000.0`), and rejection (`-1`, `0`, `abc`
  → `ValueError`). One maximal test covering all, as written in Step 5.
- **Engine** (`test_skills_test.py`): a tripped budget makes `execute_run` emit a
  `run_skipped` event and return a `_SKIPPED_ERR` result (so the cell can be marked,
  not left pending).
- **JS**: `node --check` is the syntax gate; the form/live changes are vanilla DOM and
  covered structurally by the `import ... in a._APP_JS` asserts.
- Do NOT add a paid end-to-end run. The ceiling is exercised by the unit layer; a real
  spend test is out of scope.

## Done criteria

ALL must hold:

- [ ] `ruff` clean on all five files (command above).
- [ ] `python3 test_skills_test.py` passes, including the new `run_skipped` test.
- [ ] `python3 test_skills_test_server.py` passes, including the new ceiling test.
- [ ] App JS `node --check` exits 0; `f-ceiling`, `cost_ceiling_usd`, `run_skipped`, and
      `cell.skipped` all present in `_APP_JS`.
- [ ] `grep -n "cost_ceiling_usd" skills_test_server.py` shows it read in
      `_build_run_config` and passed to `ExperimentConfig`, and NOT present in
      `_build_estimate_config`.
- [ ] No files outside the in-scope list modified (`git status`).
- [ ] `plans/README.md` status row for 023 updated to DONE.

## STOP conditions

Stop and report back (do not improvise) if:

- The "Current state" excerpts don't match the live code (drift since `b991faf`) —
  especially if `_should_stop`, the budget gate, or `setTicker`'s ceiling handling has
  changed shape.
- `setCell` rejects the `"skipped"` status in a way that needs more than adding it to an
  allow-list (e.g. a hardcoded switch that affects scoring/validity).
- Wiring the engine `run_skipped` test appears to require `git`/`claude` (the suite is
  meant to run without them) AND no existing `execute_run` test pattern exists to copy —
  report this rather than adding a networked/paid test.
- You find yourself needing to change persistence, the `cost` event shape, or anything
  that would kill an in-flight run — that's explicitly out of scope.

## Maintenance notes

For whoever owns this next:

- **Soft cap, by design.** Because the gate only blocks runs that haven't acquired a
  semaphore slot, up to `max_concurrency` runs already in flight will finish past the
  ceiling. If a hard cap is ever wanted, it must kill in-flight `claude -p` processes —
  a different, riskier feature (you'd discard paid work) that should be its own plan.
- The live ticker was already ceiling-aware (`setTicker`), so this plan deliberately
  touches no ticker code — if the ticker is refactored, keep the `ceiling_usd` field on
  the `cost` event flowing through.
- A natural follow-up (deferred): on the estimate panel, warn when the projected usage
  exceeds the entered ceiling ("projection $X > your $N cap — runs will be cut short").
  Left out here to keep scope tight; the estimate config intentionally ignores the
  ceiling.
- Reviewer should scrutinize: that `_build_estimate_config` still does NOT read the
  ceiling (else the projected run count would silently drop), and that the ceiling is
  validated as a positive number (a `0` or negative must be rejected, not treated as
  "unlimited").
