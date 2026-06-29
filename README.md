# skills-test

Measure whether a Claude Code skill actually improves agentic coding outcomes,
with statistical honesty instead of one-diff vibes.

> The CLI commands and the pip package keep their existing names (`skill-ab`,
> `skill-test`, `skill-ab-harness`) — renaming those would break installs and entry
> points. "skills-test" is the product/display name used across the app and reports.

## Requires
- Python 3.11+
- `git` and the `claude` CLI on PATH

## Try it in one command (no setup, no cost)

```
python3 skill_ab_harness.py demo         # or: skill-ab demo  (after pip/uvx install)
```

Renders a self-contained `report.html` from a bundled example —
zero `claude` calls, zero money. The HTML is an interactive, offline dashboard
(no CDN/JS deps): a comparison hero, per-arm cards, a metric selector that drives
hand-built SVG charts (per-arm means, a pairwise-effect forest plot, a per-run
distribution), a discounted blind-judge panel, and a collapsible audit trail with
the side-by-side on/off diff drill-down that explains *why* the skill helped. Light
and dark themes via `prefers-color-scheme`.

## Local app (`skill-ab serve`)

```
python3 skill_ab_harness.py serve        # or: skill-ab serve   -> opens http://127.0.0.1:7878
```

A self-contained local web app (stdlib only — no framework, no CDN, no build) that
wraps the engine: a **dashboard** of past runs, a **new-run form** with a cost/usage
**estimate gate** before anything spends, a **live view** that streams the agent's
work in real time (cell grid + console + usage ticker), and a **results** view with
a native comparison summary (effect + CI), a **Copy comparison summary** button, and
a link to the interactive report. Try it with zero spend via the **"Run the
demo"** button (replays the bundled example, streaming live).

Pick skills from a **searchable list of your installed plugins/skills** (with source
labels), and compare **models** too — set a different Model A vs Model B (e.g. the
same skill under sonnet vs opus); the report labels arms `my-skill @ sonnet` vs
`my-skill @ opus`. Toggle off the no-skill control for a cheaper pure A-vs-B run.
Set the optional **Stop after ~$N** field to cap total usage — it stops *starting*
new runs once cumulative spend crosses the threshold; in-flight runs still finish
(a soft cap on the usage proxy, not a hard dollar limit). Blank = off (the default).

Runs use **your Claude Code subscription** via `claude -p` — never an API key or the
Agent SDK; the "cost" shown is a usage proxy bounded by your plan's limits. The
server binds to `127.0.0.1` only and gates every API route behind a per-process
session token (printed on startup) plus Host/Origin checks. `--port`, `--runs-dir`,
and `--no-open` are available.

## Run it on your own skill

```
skill-ab init                 # writes skillab.toml, pre-filled from your repo + skills
# edit skillab.toml: set repo_path, skill_src, a real setup_cmd (e.g. npm ci), test_cmd
skill-ab plan -c skillab.toml --cost-per-run 0.30   # project $ and time BEFORE spending
skill-ab run  -c skillab.toml --html report.html    # the experiment + an HTML report
```

It runs each task with the skill on and off (k times each, in isolated git
worktrees), scores the results deterministically (tests/lint/build/diff/cost), and
reports. The headline number is **intention-to-treat** (all-on vs all-off — the
effect of shipping the skill); activation rate is a separate diagnostic and a
per-protocol number is reported as secondary. Significance uses a cluster bootstrap
for the CI and a cluster permutation test for the p-value, with Benjamini–Hochberg
across the secondary metrics. Raw `RunResult`s stream to `results_dir/results.jsonl`
and a portable `summary.json` (see `results.schema.json`). See `METHODOLOGY.md`.

## Commands

| Command | What it does |
|---|---|
| `demo` | offline, free example report |
| `init` | scaffold a `skillab.toml` from your repo |
| `plan` | dry-run cost/time projection + minimum-detectable-effect (no spend) |
| `run` | run the experiment; `--resume` reuses prior runs; `--from-github <url>` clones a skill repo |
| `report` | re-render an HTML report from a `results.jsonl` (no spend) |
| `ci` | run + gate with a policy exit code (see `.github/workflows/skill-ab.yml`) |

This is a **comparison benchmark, not a pass/fail build**: the report calls a
difference **significant** only when the 95% CI excludes 0 *and* the run is
trustworthy (≥2 clustered tasks, no contamination) — otherwise **inconclusive**. The
winning arm and effect size are named in the headline, never as a green/red verdict.
(The `ci` command is the one legitimate pass/fail gate — it exits non-zero on a
significant regression of the primary metric.)

## One-liner: `skill-test A B TARGET`

The fast path — no config file. Name two skills (the second can be `none` for a
skill-vs-control test) and point at a PR, branch, or directory:

```
skill-test resolve-pr-parallel resolve-pr-comments https://github.com/owner/repo/pull/7044
skill-test my-skill none .            --prompt "Add input validation to the API"
```

It resolves each skill by name (project → `~/.claude/skills` → installed plugins),
resolves the target (for a PR: fetches the PR head into a private ref — it does NOT
touch your working tree — and defaults the task to "resolve the review comments";
auto-detects the test command), then runs the 3-arm experiment + judge + HTML report.

**Isolation:** because skills under test are often globally-installed plugins, quick
mode runs each arm with `--disable-slash-commands` (suppressing *all* ambient skills)
and force-injects only that arm's `SKILL.md` guidance (`--append-system-prompt-file`),
so the comparison is clean regardless of what's installed globally. (The full `run`
command instead installs the skill into the worktree and lets it auto-activate — use
`--worktree-isolation` on `skill-test` to opt into that, only if the skills aren't
globally installed.)

## Compare two skills head-to-head (config file)

Set `skill_b_src`/`skill_b_name` in the config to pit one skill against another
(e.g. `resolve-pr-parallel` vs `resolve-pr-comments`). The experiment then runs
**three arms** — skill A, skill B, and a no-skill control — and reports **every
pairwise comparison**: each skill vs the control (does it help?) and A vs B (which
wins?). The blind judge compares the two skills' actual code output directly. The
report's headline becomes the A-vs-B comparison.

```toml
[experiment]
repo_path  = "."
base_ref   = "main"
skill_src  = "./.claude/skills/resolve-pr-parallel"
skill_name = "resolve-pr-parallel"
skill_b_src  = "./.claude/skills/resolve-pr-comments"
skill_b_name = "resolve-pr-comments"
k = 6
judge_enabled = true
```

Leave `skill_b_*` unset for the classic 2-arm skill-on vs skill-off test.

Set `judge_enabled = true` in the config to add an opt-in **blind qualitative
judge**: an LLM compares the on/off git diffs per task with arm labels stripped,
every pair judged in both orderings to cancel position bias, reported as a win-rate
with CI in a separate section marked non-deterministic (costs extra `claude` calls).

## Compare agent CLIs (claude vs codex vs any CLI)

Point an arm at a **different agent CLI** with the per-arm `runner_*` fields. `codex`
is a built-in preset (`codex exec - --sandbox workspace-write`, prompt fed on stdin);
any other value is a raw command template (use `{prompt_file}` — the prompt is passed
by *file*, never interpolated into the shell). The other arm stays on the built-in
`claude` runner.

```toml
[experiment]
repo_path  = "."
base_ref   = "main"
skill_src  = "./.claude/skills/my-skill"
skill_name = "my-skill"
runner_b   = "codex"          # arm B is the codex CLI instead of claude+skill
include_control = false       # pure A-vs-B; drop the third no-skill arm
k = 6
```

What survives a cross-CLI comparison: the **diff scorers** (tests/lint/build/diff),
the **blind judge** (it reads only the code each CLI produced, CLI-agnostic), and the
cluster bootstrap. What doesn't apply to a non-claude arm: cost/turns/activation
(claude-only — a command arm reports `cost_usd=None` and no activation).

A cross-CLI pair is **confounded by construction** — it bundles the CLI binary, its
default model, prompt handling, and a separate login/billing with any skill effect —
so the report **downgrades it to "suggestive"** (grey, never an accent "significant"
pill) and shows a banner spelling out the confound. Treat it as a lead, not a result;
use the blind judge for the cleanest read.

The **local app** exposes this too: the New-run form has an **"Agent CLI · Arm B"**
picker (Claude vs Codex) — choosing Codex disables the Skill B / Model B fields and
shows the confound caveat inline. Security: the `serve` API accepts only a **curated
preset name** (validated against the engine's `_RUNNERS` allowlist) for arm B — never a
**raw command** from a request body (that would be arbitrary code execution over
loopback), and never a runner for arm A or the control. Raw command templates
(`{prompt_file}` form) stay **config/CLI-only**.

## Tests
`python3 test_skill_ab_harness.py` — stdlib-only, no `claude`/`git` required.

See `CLAUDE.md` for design rationale, the estimand, and open work.
