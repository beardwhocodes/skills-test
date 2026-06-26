# skill-ab-harness — project context for Claude Code

## What this is
A coding-specific A/B harness that measures the effect of a single Claude Code
**skill** on agentic coding outcomes. Independent variable: whether the skill's
`SKILL.md` is present in the worktree's `.claude/skills/<name>/`. Everything else
(base commit, ambient CLAUDE.md, model, prompt, permission policy) is held constant.

Pipeline: preflight (quarantine flaky/red checks) → tasks × {skill_on, skill_off}
× k runs → isolated git worktree per run → `claude -p` headless edits it →
activation detection on BOTH arms → deterministic scorers → cluster bootstrap →
report with CIs, not point estimates.

## Design decisions (don't regress these)
- **No `--bare`.** It strips CLAUDE.md/other skills/MCP and would confound the
  comparison. Both arms run normally; only the skill dir differs.
- **Activation is detected from side effects**, not an undocumented stream-json
  "skill loaded" event (none exists / event types aren't documented). We watch for
  a `Skill` tool_use or a Read/Bash touching the skill's own SKILL.md. Keep this
  version-resilient.
- **The detector runs on the OFF arm too.** A skill firing there (global
  `~/.claude` install) = contaminated run → invalid. This is intentional.
- **Cluster bootstrap**, not flat: resample tasks then runs-within-task, so
  correlated runs from one task don't inflate significance. Needs ≥2 tasks for
  real across-task variance.

## Quick CLI (`skill-test` / `skill-ab quick`)
`skill-test <skillA> <skillB|none> <PR-url|branch|.>` (entry `quick_main`) builds the
config + one Task for you: `resolve_skill` finds a skill by name (project →
~/.claude/skills → plugin cache/marketplaces, `find -L`/case-insensitive — some ship
`Skill.md`, some are symlinks), `resolve_target` fetches a PR head into a private ref
(non-invasive) / uses a branch / dir and auto-detects test cmd.

**Isolation modes** (`ExperimentConfig.isolation`): `"worktree"` (default for `run`) =
install SKILL.md into the worktree + auto-activate; `"inject"` (default for
`skill-test`) = `--disable-slash-commands` + `--append-system-prompt-file <SKILL.md
body>`. Inject mode exists because the skills under test are usually GLOBALLY-installed
plugins that would otherwise leak into every arm. Verified against live CLI v2.1.191.
In inject mode: strip frontmatter + `` !`cmd` `` blocks before injecting
(`prepare_skill_guidance`); activation is True by construction; contamination =
`_inject_leak` (a `Skill` tool_use despite --disable-slash-commands).

## Arms (generalised)
The harness runs N arms, each defined by "install this skill (or nothing)".
- 2-arm (default): `skill_on` (cfg.skill_src) vs `skill_off` (no-skill control).
- 3-arm head-to-head: set `skill_b_src`/`skill_b_name` → `skill_on` (A), `skill_b`
  (B), `skill_off` (control); the report covers all pairwise comparisons
  (`experiment_pairs`), the badge uses `primary_pair` (A vs B), and the blind judge
  runs every pair. Resolve arms/labels via `experiment_arms` / `arm_skill` /
  `arm_label`; never hardcode the two-arm assumption.
- Validity is arm-symmetric now: `contaminated` = a FOREIGN skill fired
  (`contaminated_by`), detected per-arm via `detect_contamination`. `skill_activated`
  = the arm's OWN skill fired (None for the control). `detect_activation(events, wt,
  skill_name)` takes a name, not cfg.
- **Runner axis (plan 022, pluggable CLI):** an arm can carry a `runner_*` instead of a
  skill — `None` = the built-in `claude` runner; `"codex"` (registry preset, prompt on
  stdin via `codex exec - --sandbox workspace-write`); or a raw command template with
  `{prompt_file}` (prompt passed by FILE, never shell-interpolated). `arm_runner(cfg,
  arm)` is the seam; `run_command_agent` runs non-claude arms, `run_agent` runs claude
  (both share `_spawn_and_drain`: own process group + killpg + bounded drain). A command
  arm is claude-symmetric-but-thin: NO skill install, NO inject, NO activation/
  contamination (all claude-only), `cost_usd`/`num_turns`=None. **`agent_ok` for a
  command arm is EXPOSURE-ONLY** — `(not timed_out) and (rc is not None)`, rc-AGNOSTIC:
  a nonzero exit / empty diff is an OUTCOME the scorers capture, never a reason to drop
  the run (don't regress to `rc == 0`). **Don't regress:** (a) a cross-runner primary
  pair is **confounded** → `_pair_is_cross_runner` forces the verdict to `suggestive`
  (grey, never green/red) + a "Confounded — different agent CLIs" banner, regardless of
  CI; (b) the `serve` API accepts a curated runner PRESET NAME for arm B only (validated
  against `_RUNNERS` via `_resolve_runner_preset`), NEVER a raw command from the body
  (= loopback RCE) and never a runner for arm A / the control; raw command templates
  stay TOML/CLI-only; (c) `_SCRATCH_EXCLUDE` keeps
  every agent's scratch (`.claude/.codex/.aider*/…`) out of the judged diff so the blind
  judge stays CLI-neutral. Free shell-agent E2E + a real claude-vs-codex run are in
  `scratchpad/` (latter ~$0.18, both CLIs created the file headless).

## Estimand (don't regress this)
- **Primary = intention-to-treat.** Every clean ON run vs every clean,
  non-contaminated OFF run, REGARDLESS of whether the skill fired on the ON arm.
  This measures the effect of *shipping* the skill (= the independent variable).
- **Activation is a diagnostic, NOT an inclusion gate.** Filtering ON runs to
  only-activated ones conditions on a post-treatment outcome (selection/collider
  bias). Activation rate is reported separately; a per-protocol number exists but
  is labelled SECONDARY/biased.
- The OFF-arm contamination exclusion stays — that's exposure validity (a leaked
  global install firing), not outcome conditioning.

## Repo-specific wiring (do this first when adopting)
1. Set `ExperimentConfig.repo_path` / `base_ref` / `skill_src` / `skill_name`.
2. Put real `setup_cmd` (e.g. `npm ci`) AND `test_cmd` / `lint_cmd` / `build_cmd`
   on each `Task`. Without `setup_cmd` a fresh worktree has no deps and every
   scorer gets quarantined on the clean base.
3. Run once manually with `claude -p ... --output-format stream-json --verbose`
   and CONFIRM how a skill activation appears in YOUR CLI version; adjust
   `detect_activation()` if the fingerprint differs. Verified against current docs
   + a live run: it surfaces as a `Skill` tool_use (exact name) OR a Read/Bash of
   this worktree's own SKILL.md, and `Skill` MUST be in `allowed_tools` or it can
   never fire in headless `-p`. Re-confirm if your CLI version emits the Skill
   tool via incremental stream events rather than a complete assistant message.

## Open work (priority order)
- [x] Blind judge for qualitative axis — `run_qualitative_judge` compares ON vs
      OFF git diffs per task with arm labels stripped, judges EVERY pair in both
      orderings (on-first/off-first) to cancel position bias, and reports a
      win-rate + cluster-bootstrap CI + position-consistency in a SEPARATE section
      marked NON-DETERMINISTIC. Opt-in via `judge_enabled=True` (extra `claude`
      calls); needs `capture_diffs=True` (default on) so diffs are stored.
- [x] Multiple-comparisons correction — Benjamini–Hochberg over the secondary
      metrics; `tests_pass` pre-registered as the primary (uncorrected) endpoint.
- [x] Verify activation fingerprint against live CLI — confirmed (see wiring 3);
      detector rewritten to structured-field / exact-name / path-into-worktree
      matching so incidental reads & global copies don't false-positive.
- [x] Persist raw RunResults (JSONL) — written to `results_dir/results.jsonl`
      incrementally as each run lands.
- [x] Per-run logs/artifacts retained on failure — failed/invalid runs dump
      events + diff + stderr to `results_dir/artifacts/<label>/`
      (`keep_failed_worktrees` also leaves the tree).
- [x] `skill-ab serve` local web app (plan 021) — subscription-backed `claude -p`,
      live SSE streaming, estimate gate, skill picker, per-arm model comparison;
      loopback-only + per-process token. See README "Local app".
- [x] Pluggable CLI runner (plan 022) — compare `claude` vs `codex` vs any CLI via
      per-arm `runner_*`; cross-runner pairs downgraded to suggestive; serve refuses
      runner_* from the body. The serve UI has an "Agent CLI · Arm B" picker (curated
      preset names only). See "Arms (generalised)" above + plan 022. Deferred: aider
      preset, a parsed codex cost/turns adapter, a runner control for arm A/control,
      Docker-sandboxing the command runner for untrusted configs.

## Tests
`python3 test_skill_ab_harness.py` (stdlib-only; no `claude`/`git` needed) covers
the statistics and the activation detector. `python3 test_skill_ab_server.py` covers
the local-app server (token auth, Host/Origin, runner_* refusal, demo run, estimate).

## Safety
`--permission-mode acceptEdits` is the default. Only switch to
`--dangerously-skip-permissions` inside a container; a worktree is not a sandbox.

## Run
Now a real CLI (stdlib only; needs `git` + `claude` on PATH). `python
skill_ab_harness.py <cmd>` or `skill-ab <cmd>` after install:
- `demo` — offline, free example report+badge (zero claude calls)
- `init` / `run` / `report` / `badge` / `plan` / `ci` (see README + `plans/`)

Config is a TOML (`skillab.toml`: `[experiment]` → `ExperimentConfig`, `[[task]]` →
`Task`) loaded by `load_config`; `--example` keeps the old hand-coded path. Every run
writes `results_dir/{results.jsonl, manifest.json, summary.json}` (portable schema in
`results.schema.json`). The verdict badge is self-policing (grey unless the CI excludes
0 AND the run is trustworthy). v0.2.0 added all of `plans/` — keep `__version__` and
`pyproject.toml` version in sync.
