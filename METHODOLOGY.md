# Methodology

How `skill-ab` turns "I think my skill helped" into a number a skeptic can check.
This document exists so a result can be **cited**, not just screenshotted.

## The experiment

- **Independent variable:** whether the skill's `SKILL.md` is present in the
  worktree's `.claude/skills/<name>/`. Nothing else changes between arms (same base
  commit, ambient CLAUDE.md, pinned model, prompt, permission policy).
- For each **task × {skill_on, skill_off} × k runs**, a fresh `git worktree` is
  forked from `base_ref`, `claude -p` edits it headless, and the result is scored
  by shell commands (`tests_pass`, `lint_pass`, `build_pass`, `diff_lines`,
  `cost_usd`). `k = 1` proves nothing; use `k ≥ 5` and **≥ 2 tasks**.

## The estimand (read this before quoting a delta)

- **Primary = intention-to-treat (ITT).** The headline number compares *every*
  clean `skill_on` run against *every* clean, non-contaminated `skill_off` run —
  **regardless of whether the skill actually fired** on the ON arm. This estimates
  the real-world effect of *shipping* the skill, which is the variable you control.
- Conditioning inclusion on activation (a post-treatment outcome) is
  selection/collider bias, so the harness does **not** do it for the primary number.
  Activation rate is reported separately as a compliance diagnostic, and an
  activation-gated "per-protocol" number is shown but explicitly labelled biased.
- The **OFF-arm contamination exclusion stays**: if the skill genuinely fired on an
  OFF run (e.g. a global `~/.claude` install leaked in), that run violated its
  assignment and is dropped. That is exposure validity, not outcome conditioning.

## Significance (why the CI is trustworthy)

- **CI:** a **cluster bootstrap** — resample tasks, then runs within each task — so
  correlated runs from one task can't inflate confidence. With `< 2` shared tasks it
  degrades to flat resampling and the report flags it (`†`) as anticonservative.
- **p-value:** a **cluster permutation test** — shuffle arm labels within each task
  (the sharp null of no effect) and recompute. This is null-calibrated, fixes the
  all-ties degenerate case (constant arms → p ≈ 1.0, not 0.0), and never returns
  exactly 0 for a real effect.
- **Multiple metrics:** Benjamini–Hochberg across the secondary metrics;
  `tests_pass` is pre-registered as the single primary (uncorrected) endpoint.

## The verdict badge is self-policing

`skill-ab badge` reads `summary.json` and shows **verified/regressed** only when the
95% CI excludes 0 **and** the run is trustworthy (≥ 2 clustered tasks, **zero**
OFF-arm contamination). Otherwise it shows **inconclusive** (grey). A green badge on
an underpowered or contaminated run is impossible by construction — that's the point.

## Is my effect real, or am I underpowered?

`skill-ab plan --baseline B --noise N` reports the **minimum detectable effect**
(MDE) at your current `k` and task count, by simulating the real cluster estimator
over draws with your noise level. If your observed delta is below the MDE, a null
result is *underpowered*, not evidence of no effect — raise `k` or add tasks. A good
prior for `N` is the within-arm spread (`within_arm_noise`) from a pilot run.

## Noise floor — an OFF-vs-OFF control-vs-control run (design, plan 019)

The MDE above is a *synthetic* prior. The empirical analogue is to run **two no-skill
control arms** against each other: with no skill on either side the true effect is 0,
so any "significant" delta is a false positive. This measures the harness's actual
spurious-significance rate on *your* task/model, the most credible answer to "is my
green badge luck?". The premise is calibrated: two equal-mean controls through the
real `cluster_bootstrap_ci` exclude 0 at roughly `alpha`, not far above it (pinned by
`test_noise_floor_false_positive_rate_is_calibrated`).

- **Config / CLI surface (recommended).** A `noise_floor: bool` on
  `ExperimentConfig` (set by a `skill-ab run --null` flag) that makes
  `experiment_arms` → `[Arm.SKILL_OFF, <new Arm.SKILL_OFF_B>]`, both `arm_skill` →
  `(None, None)`, **labelled `control_a` / `control_b`**. It reuses the existing
  estimator → summary → badge → report stack unchanged, because every layer already
  iterates `experiment_arms` / `experiment_pairs`. It **cannot** reuse the `skill_b`
  fields: those require a name and `__post_init__` rejects a half-set pair, and a
  second control under today's model would collapse to `control_vs_control` (pinned by
  `test_two_controls_collapse_under_current_arm_labels`) — hence a *new* control
  Arm identity is required.
- **What it reports.** The per-task control-vs-control delta distribution, the pooled
  noise SD (`within_arm_noise` over both control arms), and the **empirical
  false-positive rate** — how often the 95% CI spuriously excludes 0. Subtlety: a
  *single* OFF-vs-OFF run is one Bernoulli draw (one delta, one CI), so the *rate*
  needs many independent control comparisons (many tasks) and/or a label-shuffle null
  over the pooled control runs — the empirical analogue of the `minimum_detectable_effect`
  loop.
- **Open questions (deferred to implementation).** (a) minimum `k` for a stable CI;
  (b) `tasks ≥ 2` is effectively required — one task drops `cluster_bootstrap_ci` to
  the anticonservative flat path, understating noise; (c) presentation — a one-line
  "noise floor: spurious-significant X% of the time; your delta = Y" annotation next
  to a real run; (d) **do not gate the badge** on the floor — keep the badge
  self-policing; at most a soft WARN when the real delta sits below the measured
  floor, never a hard gate.

This is a SPIKE conclusion (plan 019); the run mode itself is a deferred follow-up.

## Reproducibility

Every report and `summary.json` embeds a manifest: harness version, `claude --version`,
the pinned model id, `repo_path @ base_ref` resolved to an immutable SHA, the
**SHA-256 of the SKILL.md under test**, the seed, and the platform. Two runs with the
same manifest and seed are comparable; a changed SKILL.md hash means a different
experiment (and `--resume` refuses to mix them).

## What this does NOT measure

- Whether the skill's *guidance was followed* on any individual run — only the
  observable outcomes and whether the skill *activated*.
- A qualitative axis the scorers can't capture (style, clarity) — that's the opt-in,
  **non-deterministic** blind LLM judge, reported in its own clearly-marked section
  and never mixed with the hard scorers.
