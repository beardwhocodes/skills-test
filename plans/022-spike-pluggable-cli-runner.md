# Plan 022 (SPIKE + prototype): a pluggable agent runner — compare Claude vs Codex vs any CLI

> **Nature**: a DESIGN/SPIKE with a MINIMAL working prototype. It defines the runner
> abstraction, decides what survives vs drops cross-CLI, fixes the honesty/validity
> story, and ships a free-to-test command runner — NOT a polished multi-CLI product.
> This file is also the artifact an expert panel debates and refines before the build.

## Panel synthesis — BINDING decisions (4-expert debate, 2026-06-26)

The panel (systems / methodology / security / DX) found **6 blockers**. The build
implements these; the original "Core design" below is superseded where they conflict.

1. **Spawn safety (architect):** `run_command_agent` must NOT reuse `_run` (no
   `start_new_session`/killpg → codex/aider language-servers/MCP wedge the timeout).
   Factor `_spawn_and_drain(argv, cwd, timeout, stdin=None) -> (rc, out, err,
   timed_out)` out of `run_agent`; both call it. Behavior of `run_agent` unchanged.
2. **Symmetric, exposure-only `agent_ok` (methodologist):** different inclusion rules
   per runner = selection bias (CIs then measure precision, not validity). Command
   arm: `ok = (not timed_out) and (rc is not None)` — **rc-agnostic**; a nonzero-exit
   or no-diff run is an OUTCOME (`tests_pass=0`, `diff_lines=0`), never censored. Plus
   the DX guard: report per-arm "no-diff / errored" counts and **block the verdict
   when any arm produced 0 diffs across all runs** (silent-empty-diff guard).
3. **Judge blindness / metric integrity (architect+security):** the `.claude`-only
   exclude is Claude-specific; codex/aider scratch (`.codex/`, `.aider*`, caches)
   leaks into the blind diff + pollutes `diff_lines`. Apply a **generic agent-scratch
   ignore set** (+ per-runner globs) at BOTH `git add -N` and the diff. (Residual
   coding-style leak is inherent — documented; the verdict downgrade covers it.)
4. **Confounded estimand (methodologist):** "Claude+skill vs Codex" confounds skill
   AND runner in one contrast. Detect **per-pair** when a pair changes both skill and
   runner; mark it `confounded` and FORBID it as the badge `primary_pair`. Steer to
   clean estimands (skill effect = Claude+skill vs Claude-no-skill; runner effect =
   Claude-no-skill vs Codex). **Downgrade every cross-runner pair to "suggestive"** —
   never a confident green — regardless of CI.
5. **Serve = NEW RCE surface (security, verified):** today `POST /api/runs` exposes NO
   shell field (`setup_cmd`/`test_cmd` are auto-derived by `resolve_target`, never
   request-read). So a request-settable runner command = new browser-reachable RCE.
   **`_build_run_config` MUST NOT read `runner_*` from the request body** (pre-register
   now; add a test). Engine runner fields are TOML/CLI-only. Any future serve runner
   support needs an explicit **default-off launch flag**. The "no new gate" claim is
   scoped to the **local CLI/TOML** path only; `--trust-remote` must cover `runner_*`.
6. **Prompt injection + broken examples (security+DX):** `shlex.quote` is insufficient
   inside the double-quoted `"{prompt}"` examples ($(...) still fires), and PR-derived
   prompts are attacker-influenced. **Pass the prompt via stdin (codex) or a
   `{prompt_file}` temp file — never interpolated into the shell.** Ship **tested
   curated presets** (codex = `codex exec --sandbox workspace-write`, prompt on stdin)
   as the surface; raw templates are an advanced, `{prompt_file}`-only escape hatch.

Also: **model axis isolation** — `arm_model`/`_models_vary`/manifest must ignore
command arms (no `@ sonnet` on a codex arm); **label injectivity** — validate runner
labels are unique in `__post_init__`; **`wall_seconds` stays descriptive-only** (never
a scored metric/badge — it measures startup/network, not agent efficiency); **on_event**
synthetic event emitted in the command dispatch branch, not the stream mapper.

**Scope call (lead):** build the engine seam + the **codex** preset + all 6 blockers +
the confounded-pair/verdict-downgrade honesty + serve refusal, with tests, a free
shell-agent E2E, and ONE bounded real codex run. DEFER: aider preset, the serve UI
runner control (behind the default-off flag), Docker sandbox, parsed codex adapter.

> **Repo NOT git-initialized** — date-stamp, compare excerpts (no SHA).

## Status

- **Priority**: P2 (the "bigger bet" after per-arm models, which already landed)
- **Effort**: M (spike + minimal prototype)
- **Risk**: MED (runs user-provided commands as "agents"; cross-CLI validity is subtle)
- **Depends on**: per-arm models (done) — reuses the arm-carries-config refactor
- **Category**: direction / architecture
- **Planned at**: not git-initialized, 2026-06-26

## Why this matters

Today an arm = `(skill, model)`, always run by `claude -p`. The natural next axis is
the **agent itself**: "does Codex write better code than Claude on this task?" or "my
shell-script agent vs Claude". The measurement spine is already agent-agnostic — a
task runs in a git worktree, we `git diff`, score with `tests/lint/build`, cluster-
bootstrap, and a **blind judge** compares the two diffs with labels stripped. What's
Claude-coupled is the *invocation + output parsing* (`run_agent` parses Claude's
`stream-json` for cost/turns/activation). The bet: abstract the **runner** so any CLI
that edits a worktree can be an arm, while keeping the stats/report/judge intact.

## Core design

### The runner is the new axis; the arm becomes `(runner, skill, model)`

A **runner** answers: "given a worktree + a task prompt, make edits." Two kinds:

1. **`claude`** (built-in, default) — the existing `run_agent`: `claude -p` with
   stream-json, so it yields cost, turns, tool_use **events** (→ skill activation /
   contamination), and a per-arm model. Unchanged.
2. **`command`** (new, generic) — a user-supplied shell template, e.g.
   `codex exec "{prompt}"`, `aider --yes --message "{prompt}"`, or a trivial
   `sh -c '...'`. We substitute the (shell-quoted) prompt, run it in the worktree,
   then read the diff from git like always. CLI-agnostic by construction.

Per-arm runner overrides on `ExperimentConfig`, parallel to `model_a/model_b`:
`runner_a`, `runner_b`, `runner_off` — `None` ⇒ `claude`; a string ⇒ a `command`
template. So "Claude+skill vs Codex" = `skill_a=<skill>, runner_a=None`,
`skill_b=none, runner_b="codex exec \"{prompt}\""`.

### What survives, what drops (the honest contract)

| Signal | claude runner | command runner |
|---|---|---|
| git diff, **tests_pass / lint / build / diff_lines** | ✅ | ✅ (the whole point) |
| **blind judge** (diffs, labels stripped) | ✅ | ✅ — *the cross-CLI equalizer* |
| cluster-bootstrap CIs / permutation / BH | ✅ | ✅ (metric-agnostic) |
| **cost_usd / num_turns** | ✅ (from stream-json) | ❌ `None` (not comparable across billing systems) |
| **skill activation / contamination** | ✅ | ❌ N/A — the Claude-skill concept doesn't exist in Codex; a command arm is treated like a "no-skill external" arm |

So a command-runner arm is, in validity terms, a **no-skill arm whose agent is an
external CLI**: `agent_ok` = "the command exited 0 and produced a diff"; `activated`,
`contaminated_by`, `cost_usd`, `num_turns` are `None`.

### Honesty / validity (must be surfaced, like single-task smoke tests are)

- **You're comparing agent CLI *defaults***, not models in a vacuum — different tools,
  permissions, context, prompt conventions. The result is *suggestive*; the report
  must say so wherever a cross-runner comparison is shown.
- **Auth/billing is per-CLI.** Claude rides the user's Claude subscription; Codex
  rides OpenAI's; etc. The "subscription, not API key" guarantee holds **only** for the
  claude runner. The tool can't and shouldn't manage other CLIs' auth.
- **Cost is dropped** for command arms (incomparable), so cross-CLI verdicts lean on
  `tests_pass` / `diff_lines` / the judge — fine, that's what those exist for.

### Engine refactor (concrete)

- `arm_runner(cfg, arm) -> str | None`: the arm's runner override (`None` = claude).
- `_runners_vary(cfg)`: any non-None runner override.
- `arm_label`: when runners vary, disambiguate by a short runner name (the command's
  first token, e.g. `codex`; `claude` for the built-in) just like model does —
  `my-skill @ claude` vs `codex`.
- `run_command_agent(worktree, task, cfg, template, timeout) -> dict`: shell-quote the
  prompt into `template`, run via the existing `_run` (subprocess, cwd=worktree,
  timeout, killpg), return `{ok: rc==0, events: [], completed: True, timed_out,
  cost_usd: None, num_turns: None, hit_turn_limit: False, stderr, transcript: stdout}`.
- `execute_run` (line ~1008): **dispatch on `arm_runner(cfg, arm)`**. If `None`/claude
  → existing path (skill install/inject, `run_agent`, activation/contamination). Else
  → skip skill install + inject entirely, call `run_command_agent`, and set
  `activated=None, reason="<runner>: external CLI (no skill/activation)",
  contaminated_by=None`. The `git add -N` diff-capture + scoring run unchanged for both.
- `experiment_manifest`: record `arm_runners` (like `arm_models`).
- The `on_event` live stream: a command arm emits a single `agent {kind:"text"}` with
  a truncated transcript tail (no per-event stream), then `run_done`.

### Config / CLI / UI surface (staged; prototype does the engine + a test)

- Engine: `runner_a/runner_b/runner_off` fields (prototype: yes).
- `skillab.toml`: `runner_a = "codex exec \"{prompt}\""` (prototype: parsed if present).
- `serve` UI: a per-arm **Runner** control (Claude default | custom command), with the
  command field + examples for codex/aider/cursor, plus a banner "comparing agent CLIs
  — suggestive; each CLI uses its own login/billing." (prototype: deferred to a
  follow-up; the engine + a TOML/quick path is enough to prove the spike.)

### Security / trust model (no new gate needed, but be explicit)

A command runner runs a shell command — but the harness **already** runs user-provided
`setup_cmd`/`test_cmd` via `shell=True` for local configs. So a *local* runner command
is the same trust as a local setup_cmd (the user's own machine, their own config). The
only escalation is `--from-github`: a remote config could set a malicious
`runner_b` → RCE — but that path **already** runs remote `setup_cmd` and is **already**
gated behind `--trust-remote`. So: reuse the existing gate; the spike adds NO new
capability beyond what `--trust-remote` already accepts. Document this; do not invent a
second gate. (The serve UI runner command comes from the local user typing it — same as
typing a setup_cmd.)

## Minimal prototype scope (what the build actually ships)

1. Engine: `runner_a/runner_b/runner_off`, `arm_runner`, `_runners_vary`, `arm_label`
   runner disambiguation, `run_command_agent`, `execute_run` dispatch (+ skip
   activation for non-claude), `arm_runners` in the manifest. Backward-compatible
   (`runner_*` all None ⇒ today's behavior; **all 96 tests stay green**).
2. Tests (free, stdlib): `run_command_agent` runs a real trivial `sh -c` in a tmp dir
   and returns the right shape (ok, no cost/turns, transcript); `arm_label`/manifest
   reflect a runner; backward-compat.
3. A **free end-to-end harness check** (manual script, not the unit suite): a throwaway
   git repo + a command-runner arm whose "agent" is a trivial shell command that edits
   a file → full pipeline → a report with `claude`-ish arm vs `command` arm, **zero
   model spend**. This proves cross-runner end to end without any CLI/auth/cost.

## Open questions for the expert panel (resolve before/within the build)

1. **Cost handling cross-CLI**: drop it entirely (current plan) vs. attempt a
   normalized "turns"/"wall-time" proxy? Does dropping cost weaken the report's verdict
   logic anywhere (badge reads `tests_pass`, so likely fine — confirm)?
2. **Estimand/validity**: is "Claude+skill vs Codex" a coherent comparison, or only
   "Claude(no skill) vs Codex"? Should the tool *warn* when a skill is on one arm and a
   non-claude runner on the other (confounded: skill AND agent differ)?
3. **Prompt fidelity / fairness**: same prompt to every CLI undersells whichever
   expects a different invocation. Do we expose per-runner prompt templating, or accept
   "defaults comparison" and label it loudly?
4. **`{prompt}` substitution safety**: shell-quote (shlex) vs pass via stdin vs a temp
   file — which avoids breaking on long/multiline prompts and injection within the
   user's own command?
5. **Runner identity for the judge**: the judge must stay blind — confirm the runner
   name never leaks into the judged diff (it shouldn't; diffs are file changes only).
6. **Failure semantics**: a CLI that exits non-zero but DID edit files — is that
   `agent_ok`? (claude requires a terminal result event; command runner uses rc==0 —
   should we instead treat "produced a diff" as success?)
7. **Scope creep guard**: do we hardcode a `codex` adapter (nicer UX, parses Codex
   output for turns) or keep it purely generic (template only)? Recommend generic for
   the spike.

## Commands

| Purpose | Command | Expected |
|---|---|---|
| Tests | `python3 test_skill_ab_harness.py` ; `python3 test_skill_ab_server.py` | both `N passed` |
| Lint | `uvx ruff check skill_ab_harness.py skill_ab_server.py skill_ab_app.py test_*.py` | clean |
| Free E2E | the throwaway-git-repo + shell-agent script (no claude/codex) | a report with 2 runners |
| Real (bounded) | one tiny `claude` vs `codex` run, k=1, 1 trivial task | a real cross-CLI report |

## Test plan

- Unit: `run_command_agent` shape (free); `arm_label`/manifest runner; backward-compat.
- Free E2E: shell-agent through the real `run_experiment` on a tmp git repo (no spend).
- **Real (bounded by the usage cap the operator set):** ONE minimal `claude` vs `codex`
  run on a trivial task (k=1) to confirm a true cross-CLI comparison renders. Frugal:
  smallest task, k=1, abort if it looks costly; lead with the free path.

## Done criteria (spike) — ALL MET (2026-06-26)

- [x] `arm_runner`/`run_command_agent`/dispatch exist; non-claude arms skip activation
      and carry `cost_usd=None`. (`execute_run` dispatches on `arm_runner`; verified by
      the real run: codex arm came back `cost=None`, `activated=None`, `ok=True`.)
- [x] All existing tests pass + new runner tests; `ruff` clean. (95 engine + 12 server
      tests pass; `ruff` clean on all 5 files; both embedded JS bundles `node --check`.)
- [x] The free shell-agent E2E renders a report comparing a claude-style arm and a
      command arm with zero model spend. (`scratchpad/e2e_command_runner.py`: bash-agent
      vs sh-control, real git diffs scored, cross-runner banner, 0 spend.)
- [x] One bounded real `claude` vs `codex` run renders.
      (`scratchpad/real_claude_vs_codex.py`: 2 runs in 37s, ~$0.18 total under a $2
      ceiling; both CLIs created `hello.txt` headless; codex ran via `codex exec -
      --sandbox workspace-write` with the prompt on stdin; report shows the suggestive
      downgrade.)
- [x] The report/UX surfaces the cross-CLI validity caveat. (Primary-pair verdict is
      forced to `suggestive` — grey, never green/red — with a "Confounded — different
      agent CLIs" banner listing each arm's CLI; `_pair_is_cross_runner` gates it.
      Serve refuses `runner_*` from the request body — runners are TOML/CLI-only.)

### Blocker status (panel synthesis, all addressed)
1. `_spawn_and_drain` shared by `run_agent` + `run_command_agent` — done.
2. Exposure-only `agent_ok = (not timed_out) and (rc is not None)`, rc-agnostic — done
   (real codex nonzero-safe; unit `test_run_command_agent_exposure_only_ok_on_nonzero_exit`).
3. Generic agent-scratch ignore set `_SCRATCH_EXCLUDE` keeps the judged diff CLI-neutral — done.
4. Cross-runner pair downgraded to suggestive + bannered, never a badge green — done
   (`_verdict_blob(cross_runner=True)`, `crossRunnerBanner()`, render test).
5. Serve does NOT read `runner_*` from the request body — done (comment + server test).
6. Prompt via stdin (preset) / shell-quoted `{prompt_file}` (raw), never interpolated —
   done (injection unit tests + real stdin path).

## STOP conditions

- A cited engine signature drifted.
- Making a command runner work appears to require an API key / the Agent SDK (it does
  not — it's a subprocess that edits a worktree).
- A real run would clearly exceed the operator's usage cap — STOP, keep the free path.
- The refactor can't keep activation/contamination strictly claude-only without
  touching the stats — STOP (validity regression).

## Maintenance notes

- The runner abstraction is the seam; new runners are new `arm_runner` values + a
  branch in `execute_run`. Keep the stats/scorers/judge runner-agnostic.
- Anything claude-specific (cost, turns, activation, inject mode) stays behind the
  claude branch; never assume it for a generic runner.
- Follow-ups (deferred): per-runner prompt templates; a parsed `codex` adapter for
  turns/cost parity; the serve UI runner control; Docker-sandbox the command runner for
  untrusted/remote configs.
