# Plan 021: `skills-test serve` — a polished, local-first web app over the harness

> **Status / nature**: This is a BUILD plan for a full feature (not a spike). It is
> also the **coordination spec** for parallel builders: it fixes the file
> boundaries, the HTTP/SSE API contract, the engine-hook signatures, the security
> model, and the test matrix so a senior-engineering agent (backend), a design
> agent (frontend), and a testing agent can work in parallel without colliding.
>
> **Repo is NOT git-initialized** — no SHA stamping; "drift check" = compare the
> Current-state excerpts to live code before editing.

## Status

- **Priority**: P1 (the productization the user asked for)
- **Effort**: L
- **Risk**: MED (new network surface that spawns paid agents + edits files)
- **Depends on**: none (builds on the existing engine; 007–020 already landed)
- **Category**: direction / feature
- **Planned at**: not git-initialized, 2026-06-25

## Why this matters

The harness already has the hard parts — a reproducible measurement engine, a
self-contained interactive `report.html`, a portable `summary.json`, a badge, and
a gallery prototype. What it lacks is a **UX shell**: a way to configure a run,
**watch it happen live**, see the cost/usage *before* spending, browse past runs,
and open results — without hand-editing TOML or reading CLI output. This plan adds
`skills-test serve`: a local web app at `http://127.0.0.1:<port>` that wraps the
engine. It is **local-first by design** (the project already rejected hosted
SaaS/accounts) and **subscription-billed** (it drives `claude -p` under the user's
Claude Code login — NOT the Agent SDK, which requires a metered API key).

## Non-negotiable constraints (every builder must honor)

1. **Subscription, not API tokens.** Runs spawn `claude -p` exactly as the engine
   does today (`run_agent`), which inherits the user's Claude Code OAuth login.
   Do **NOT** use the Anthropic Agent SDK or `ANTHROPIC_API_KEY` anywhere in the
   serve path. The app's "cost" is a **usage proxy**, bounded by the user's plan's
   rate/usage limits, not billed as dollars — surface it that way.
2. **Stdlib-only.** No Flask/FastAPI/uvicorn, no npm/build step, no CDN assets, no
   frontend framework. Backend = `http.server` (`ThreadingHTTPServer`) + `asyncio`
   for the engine + `threading`/`queue` for SSE fan-out. Frontend = vanilla ES5/ES6
   in `<script>` strings + inline CSS reusing the report's `_HTML_STYLE` design
   system. This preserves `uvx skills-test serve` zero-install. Python ≥ 3.11.
3. **Security (this server spawns paid, file-editing agents — treat it as such).**
   - Bind to **127.0.0.1 only** (never 0.0.0.0).
   - Generate a random per-process **session token**; embed it in the served app
     shell; require it (`X-Skill-AB-Token` header or `?token=`) on **all** `/api/*`
     routes. Missing/wrong → `403`.
   - **DNS-rebinding defense**: reject any request whose `Host` header is not
     `127.0.0.1:<port>` / `localhost:<port>` → `403`. On mutating (POST) requests
     also reject a cross-origin `Origin` header.
   - Never echo a secret value anywhere (the token is a session nonce, fine to
     show; never log Claude auth/tokens).
4. **The engine stays the source of truth.** The server orchestrates the existing
   `run_experiment`/`summary_dict`/`build_html_report`/`estimate_cost`/`build_gallery_html`
   functions; it must not re-implement statistics, scoring, or rendering.
5. **Cost gate.** A real run is only startable **after** an estimate is shown and
   explicitly confirmed. The estimate reuses `estimate_cost` / `minimum_detectable_effect`.
6. **Comments explain WHY**; lines < 100 cols (ruff line-length 100); the existing
   80-test suite must stay green; `uvx ruff check` must stay clean on all files.

## Architecture & file ownership (the parallel boundaries)

Four files. Owners in brackets — build in parallel against the contracts below.

- **`skill_ab_harness.py`** (engine) — **[FOUNDATION, done first by the lead]**
  adds: an optional `on_event` progress callback threaded
  `run_experiment → execute_run → run_agent`; a `discover_runs(root)` history
  helper; and the `serve` subcommand wiring in `main()` that imports and calls the
  server module. Backward-compatible (`on_event=None` ⇒ today's behavior).
- **`skill_ab_server.py`** (NEW) — **[senior backend agent]** the HTTP server:
  `ThreadingHTTPServer` subclass, a `_Handler(BaseHTTPRequestHandler)` with the
  routes below, a thread-safe `RunRegistry` (active runs + per-run SSE subscriber
  queues), security middleware (token + Host/Origin checks), the background-thread
  run launcher that wires `on_event` into the registry, and the **demo/replay**
  launcher. Exposes `serve(host, port, runs_dir, open_browser, token=None)`.
- **`skill_ab_app.py`** (NEW) — **[design agent]** the frontend: `app_shell_html(token)`
  returning ONE self-contained SPA (router + 5 views) as Python strings; `_APP_CSS`
  (reuse `skill_ab_harness._HTML_STYLE` for the design tokens, add app chrome:
  sidebar, top bar, forms, live console, cell grid); `_APP_JS` (vanilla, talks to
  the API, renders the SSE stream). No external assets; dark mode via the inherited
  `prefers-color-scheme`.
- **`test_skill_ab_server.py`** (NEW) — **[testing agent]** stdlib-only tests for
  routing, token auth, Host/Origin rejection, `RunRegistry` fan-out, SSE event
  framing, `discover_runs`, the estimate endpoint, and the demo/replay run
  end-to-end (no `claude`, no spend). Plus engine-hook tests added to
  `test_skill_ab_harness.py`.

`pyproject.toml`: add `skill_ab_server` and `skill_ab_app` to `py-modules`; keep
`skills-test`/`skills-test quick` scripts. Bump `__version__` + `pyproject` to `0.3.0`.

## HTTP / SSE API contract (v1 — FROZEN; build to this)

All `/api/*` require the token (except `GET /api/health`, which is read-only and
token-checked too but never mutating). JSON in/out unless noted.

| Method | Path | Body / Query | Returns |
|---|---|---|---|
| GET | `/` | `?token=` optional | app shell HTML (token injected) |
| GET | `/api/health` | — | `{ok, claude_on_path, claude_version|null, runs_dir, harness_version, model}` |
| GET | `/api/runs` | — | `{runs: [RunCard]}` (newest first) |
| GET | `/api/runs/<id>/summary` | — | the run's `summary.json` |
| GET | `/api/runs/<id>/report` | — | `text/html` (the run's `report.html`) |
| GET | `/api/runs/<id>/badge` | — | `image/svg+xml` (the run's `badge.svg`) |
| GET | `/api/gallery` | — | `text/html` (`build_gallery_html` over all runs) |
| POST | `/api/estimate` | `EstimateReq` | `{n_runs, projected_usd, projected_wall_seconds, n_judge_calls, note}` |
| POST | `/api/runs` | `StartReq` | `{run_id}` (202) — starts background run |
| GET | `/api/runs/<id>/events` | — | **SSE** stream of `Event` objects |
| POST | `/api/runs/<id>/abort` | — | `{aborted: true}` |

`RunCard` = `{id, title, skill_a, skill_b|null, target, status, verdict|null,
primary_metric, created_ts, cost_usd, n_valid, report_url, badge_url}`. `status` ∈
`{running, done, error, aborted}`.

`EstimateReq`/`StartReq` = `{skill_a: str, skill_b: str|null, target: str, k: int,
isolation: "inject"|"worktree", judge: bool, demo: bool}`. `target` is a PR URL /
branch / `"."`. `demo:true` ⇒ replay the bundled demo, **no spend, no claude**.

**SSE `Event` schema** (one JSON object per `data:` line; `type` discriminates):
- `experiment_start` `{cells: [{label, task, arm, idx}], arms: [str]}`
- `run_start` `{label}`
- `agent` `{label, kind: "text"|"tool"|"result", text?, tool?, cost_usd?, turns?}`
- `run_done` `{label, itt_valid, activated, contaminated_by|null, cost_usd, turns, diff_lines}`
- `cost` `{spent_usd, ceiling_usd|null}`
- `experiment_done` `{run_id, verdict|null, primary_metric, report_url, badge_url, summary_card}`
- `error` `{message}`  (also closes the stream)

The server keeps a bounded backlog per run so a late-subscribing `EventSource`
replays from the start (a reconnect during a run must catch up, not miss cells).

## Engine hooks (FOUNDATION — exact signatures; lead implements first)

In `skill_ab_harness.py`, all additive and backward-compatible:

1. `run_experiment(cfg, tasks, scorers=None, resume=False, on_event=None)` — when
   `on_event` is given, call `on_event(evt: dict)` at: experiment start (after
   preflight, with the cell list), each cell start/done, a `cost` update after each
   persist, and experiment done. Thread `on_event` into `run_and_persist` →
   `execute_run`.
2. `execute_run(task, arm, idx, cfg, scorers, pf, sem, budget, on_event=None)` —
   emit `run_start`/`run_done`; pass `on_event` into `run_agent`.
3. `run_agent(worktree, task, cfg, inject_file=None, disable_skills=False, on_event=None)`
   — for each parsed stream-json message, if `on_event`, emit a SUMMARIZED `agent`
   event (assistant text snippet / tool name / final cost+turns). Do NOT dump raw
   stream bytes; summarize to the `Event` schema. `on_event` must never raise into
   the run (wrap in try/except; a UI subscriber dying can't break a paid run).
4. `discover_runs(root: Path) -> list[dict]` — scan `root` for immediate
   subdirectories containing `summary.json`; return `RunCard`-shaped dicts sorted
   newest-first (use `manifest.timestamp` then mtime). Reuse
   `_gallery_verdict_from_summary` for the verdict. Tolerate malformed/partial run
   dirs (skip, never raise).
5. `main()`: add `sub.add_parser("serve", ...)` with `--port` (default 7878),
   `--runs-dir` (default `~/.skills-test/runs`), `--no-open`. Dispatch:
   `from skill_ab_server import serve; serve(...)`. Import lazily inside the branch
   so the engine has no hard dependency on the server module.

The server assigns each run a `run_id` and a `results_dir = runs_dir/<run_id>/`;
it builds the `ExperimentConfig` with that `results_dir`, runs the experiment with
an `on_event` that publishes to the registry, then writes `report.html` +
`summary.json` + `badge.svg` into the run dir (reusing `_run_and_outputs`'s logic
or calling `build_html_report`/`summary_dict`/`render_badge_svg` directly).

## Frontend surfaces (design agent — 5 views, one SPA)

Reuse the report's visual language (cards, badges, `--good/--bad/--muted` tokens,
dark mode). A left sidebar (Dashboard / New run / Gallery / Settings) + a top bar
(app title, health dot = claude-logged-in?, the run cost ticker when active).

1. **Dashboard** — grid of `RunCard`s (badge, skill A vs B, target, verdict pill,
   cost, date, status). Click → Results. Empty state → "Run the demo" CTA.
2. **New run** — form: skill A (text), skill B (text or "none = control"), target,
   k slider, isolation, judge toggle, plus a prominent **"Estimate"** button that
   calls `/api/estimate` and shows projected usage/time/runs with the
   subscription-usage note; **"Start" is disabled until an estimate is shown**, and
   a separate **"Try the demo (no spend)"** button posts `demo:true`.
3. **Live run** — opened on Start: a **cell grid** (one tile per task×arm×k,
   colored by status: pending/running/valid/invalid/contaminated), a **live
   console** rendering the `agent` events (assistant text + tool calls, per arm,
   color-coded), a **usage ticker** (cumulative cost_usd vs ceiling), and an
   **Abort** button. On `experiment_done` → a "View report" CTA. Reconnect-safe
   (EventSource replays backlog).
4. **Results** — embeds the run's `report.html` (iframe to `/api/runs/<id>/report`)
   + the badge + a copy-badge-markdown button. Link back to Dashboard.
5. **Settings / Health** — claude on PATH? version? a "you appear logged in / not
   logged in" hint (from `/api/health`); runs dir; default model/k; a one-paragraph
   note that all runs use the Claude **subscription**, not the API.

Accessibility/polish: keyboard-focusable controls, responsive ≥ 700px, no layout
shift on SSE updates, a clear "demo (no spend)" badge on demo runs.

## Demo / replay mode (the verification + onboarding vehicle)

`POST /api/runs {demo:true}` must run with **zero** `claude`/spend: the server
replays `_demo_results()` as a timed sequence of `experiment_start` → per-cell
`run_start`/`agent`(synthetic text from the stored diffs)/`run_done` → `cost` →
`experiment_done`, writes a real demo run dir (report+summary+badge via the demo
path), and tags the `RunCard` `demo:true`. This makes the entire UI — including the
live console and cell grid — demonstrable offline, and is the primary E2E test
fixture (tests drive a demo run and assert the event sequence + the written run
dir).

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Tests | `python3 test_skill_ab_harness.py` ; `python3 test_skill_ab_server.py` | both end `N passed`, exit 0 |
| Lint | `uvx ruff check skill_ab_harness.py skill_ab_server.py skill_ab_app.py test_skill_ab_server.py` | `All checks passed!` |
| Smoke | `python3 skill_ab_harness.py serve --port 7879 --no-open &` then `curl -s localhost:7879/api/health` | `{"ok": true, ...}` |
| Demo E2E | `curl -s -XPOST localhost:7879/api/runs -H "X-Skill-AB-Token: <tok>" -d '{"demo":true,...}'` | `{"run_id": ...}`, events stream |

## Scope

**In scope**: `skill_ab_harness.py` (hooks + serve wiring), `skill_ab_server.py`
(new), `skill_ab_app.py` (new), `test_skill_ab_server.py` (new), engine-hook tests
in `test_skill_ab_harness.py`, `pyproject.toml` (modules + version), `README.md`
(a "Local app" section). **Out of scope** (record as follow-ups, do not build):
Docker sandboxing of `--from-github` runs; desktop (Tauri/Electron) packaging;
multi-user/auth beyond the localhost token; hosted gallery. The badge/estimand/
stats logic is untouched.

## Steps (sequencing for the parallel build)

1. **[lead] Foundation** — implement the engine hooks (signatures above) +
   `discover_runs` + the `serve` subcommand stub that imports `skill_ab_server`.
   Keep all 80 tests green (`on_event=None` default). This unblocks the rest.
2. **[parallel] Backend / Frontend / Tests** — three agents build
   `skill_ab_server.py`, `skill_ab_app.py`, and `test_skill_ab_server.py` against
   the frozen API + Event contract. The server imports `app_shell_html` from
   `skill_ab_app`; both import only from `skill_ab_harness`.
3. **[lead] Integrate & verify** — run `skills-test serve`, hit `/api/health`, drive a
   **demo run**, confirm the SSE event sequence and the written run dir; open the
   app in a browser, screenshot every view, iterate on design until polished.
4. **[parallel] Review** — a senior backend reviewer (security: token/Host/Origin,
   thread-safety of `RunRegistry`, no API-key/SDK leak, `on_event` can't break a
   run), a design reviewer (visual QA on the screenshots), and a test auditor
   (coverage of the security + SSE + demo paths). Apply fixes.
5. **[lead] Deliver** — `README.md` "Local app" section; `__version__`/pyproject
   `0.3.0`; final `python3 test_*` + `uvx ruff check` green; a screenshot tour.

## Test plan

- **Engine** (`test_skill_ab_harness.py`): `on_event` receives an ordered event
  stream from a stubbed run (monkeypatch `run_agent`/`execute_run` collaborators,
  not the whole `execute_run`); `discover_runs` finds/sorts/【tolerates-malformed】
  run dirs; the `serve` subcommand parses args without importing claude.
- **Server** (`test_skill_ab_server.py`, stdlib `http.client` against a real
  `ThreadingHTTPServer` on an ephemeral port): `/api/health` ok; missing/wrong
  token → 403; bad `Host` header → 403; cross-`Origin` POST → 403; `/api/estimate`
  returns the projected fields; a **demo** `POST /api/runs` returns a `run_id` and
  the `GET .../events` SSE yields the documented sequence ending in
  `experiment_done`; `discover_runs`/`/api/runs` lists the demo run; abort works.
- **No `claude`/`git`/network** in any test (demo mode + stubs only).
- Verification: both suites pass; `uvx ruff check` clean; the live browser smoke in
  Step 3 renders all five views with no console errors.

## Done criteria

- [ ] `python3 skill_ab_harness.py serve --port <p> --no-open` starts; `/api/health`
      returns `{"ok": true}`; binding is 127.0.0.1 only.
- [ ] A **demo** run completes end-to-end in the browser: cell grid fills, live
      console streams, usage ticker moves, report opens — **zero spend**.
- [ ] Security: no-token `/api/runs` POST → 403; foreign `Host` → 403; cross-`Origin`
      POST → 403. No `ANTHROPIC_API_KEY` / SDK reference anywhere in the serve path.
- [ ] `python3 test_skill_ab_harness.py` and `python3 test_skill_ab_server.py` both
      pass; `uvx ruff check` clean on all four files.
- [ ] `pyproject.toml` lists the new modules; `__version__` == `0.3.0` in both files.
- [ ] No external/CDN asset, no framework, no API-key path (stdlib + subscription).

## STOP conditions

- A cited engine signature no longer matches live code (drift).
- The only way to stream live agent output appears to require the Agent SDK or an
  API key — STOP (it does not; `run_agent` already parses `claude -p` stream-json).
- A real `claude` run is needed to pass a test — STOP (use demo/replay + stubs).
- Binding to anything other than 127.0.0.1, or shipping an endpoint that mutates
  without the token + Host check — STOP and fix before proceeding.

## Maintenance notes

- The server is a thin orchestration layer; all stats/render stay in the engine.
- `on_event` is best-effort and must never raise into a run — a dead UI subscriber
  cannot abort a paid experiment.
- Follow-ups (deferred, recorded): Docker-sandbox the `--from-github` run path
  before exposing "run someone else's skill" in the UI; desktop packaging; a
  publish-to-gallery action once the trust/verification story exists.
