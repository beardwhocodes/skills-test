"""
skill_ab_harness.py
====================

A robust, coding-specific A/B harness for measuring the effect of a single
Claude Code *skill* on agentic coding outcomes.

Independent variable: whether `SKILL.md` is present in the worktree's
`.claude/skills/<name>/`. Everything else (base commit, ambient CLAUDE.md,
model, prompt, permission policy) is held constant across arms.

Pipeline:

    preflight (install deps, quarantine flaky / already-red checks)
      -> tasks x {skill_on, skill_off} x k runs
      -> isolated git worktree per run (deps installed via setup_cmd)
      -> claude -p headless edits the worktree
      -> ACTIVATION DETECTION on BOTH arms (compliance + contamination guard)
      -> deterministic Scorers emit hard numbers (tests / lint / build / diff)
      -> INTENTION-TO-TREAT estimate (all-on vs all-off) is the primary number
      -> CLUSTER BOOTSTRAP for the CI, CLUSTER PERMUTATION for the p-value
      -> report with distributions + significance, not point estimates

Estimand (the headline number):
  * PRIMARY = intention-to-treat (ITT): every clean ON run vs every clean,
    non-contaminated OFF run, REGARDLESS of whether the skill fired on the ON
    arm. This estimates the real-world effect of *shipping* the skill, which is
    the stated independent variable. Conditioning ON-arm inclusion on activation
    (a post-treatment outcome) is selection/collider bias, so we do NOT do it for
    the primary estimate.
  * activation rate is reported as a separate first-stage / compliance diagnostic.
  * a per-protocol ("conditioned on activation") number is reported too, but is
    explicitly labelled SECONDARY and potentially biased.

Robustness properties:
  * Activation is detected from the skill's observable side effects in the event
    stream. In a regular `claude -p` session only a skill's name+description load
    at startup; the BODY loads when the skill is invoked, surfacing as either a
    `Skill` tool_use naming this skill OR a Read/Bash of this worktree's own
    SKILL.md. Both are matched against STRUCTURED tool inputs (not a blob), with
    exact name equality and path-resolved-into-this-worktree checks, so an
    incidental grep/cat, a prompt echo, a global ~/.claude copy, or a
    substring-colliding skill name cannot produce a false activation.
  * The `Skill` tool is on the allowlist; otherwise it could never fire in
    headless -p and every ON run would falsely read as "did not activate".
  * The same detector runs on the OFF arm: if the skill GENUINELY fired there
    (e.g. a global ~/.claude install), that run is contaminated -> excluded.
  * Flaky/nondeterministic checks are quarantined before the experiment.
  * The model is pinned to a dated/dateless snapshot, never a drifting alias.
  * Significance: cluster bootstrap (resample tasks, then runs within task) for
    the CI; a cluster permutation test (shuffle arm labels within each task) for a
    null-calibrated p-value; Benjamini-Hochberg across the secondary metrics.

Requires: Python 3.11+, git, and the `claude` CLI on PATH.
Repo-specific wiring: the scorer commands AND `setup_cmd` on each Task, plus a
one-time confirmation of the activation fingerprint against your CLI version
(see CLAUDE.md wiring step 3).
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import html
import json
import os
import platform
import random
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import tomllib
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

__version__ = "0.4.0"   # keep in sync with pyproject.toml

# Curated agent runners (plan 022): an arm whose runner is one of these (or a raw
# command template) runs an EXTERNAL CLI in the worktree instead of `claude -p`. The
# prompt is passed via stdin (never the shell). Drops cost/turns/activation (those are
# Claude-only); cross-runner comparisons are "suggestive" by construction.
_RUNNERS: dict[str, dict] = {
    "codex": {
        "label": "codex",
        # `codex exec - --sandbox workspace-write`: read the prompt from stdin (`-`)
        # and edit the worktree. Rides the user's ChatGPT/OpenAI login + billing, NOT
        # the Claude subscription -- the user installs + auths codex themselves.
        "argv": ["codex", "exec", "--sandbox", "workspace-write", "-"],
    },
}
# Agent scratch the diff/judge must IGNORE: else a runner's artifacts (.codex/, .aider*)
# un-blind the judge by filename and inflate diff_lines. Applied at every diff site.
_AGENT_SCRATCH: tuple[str, ...] = (".claude", ".codex", ".aider*", ".cursor", "*.orig")
_SCRATCH_EXCLUDE = " ".join(f"':(exclude){g}'" for g in _AGENT_SCRATCH)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class Arm(str, Enum):
    SKILL_ON = "skill_on"     # the (primary) skill: cfg.skill_src / cfg.skill_name
    SKILL_OFF = "skill_off"   # the no-skill control
    SKILL_B = "skill_b"       # a SECOND skill (head-to-head): cfg.skill_b_src / skill_b_name


@dataclass(frozen=True)
class Task:
    """One coding problem thrown at both arms."""
    id: str
    prompt: str
    # Shell commands run inside the worktree.
    # setup_cmd runs ONCE right after checkout (before the agent) to install deps
    # etc. A fresh `git worktree add` has no node_modules / venv / build state, so
    # without this the scorers fail on the clean base and get quarantined.
    setup_cmd: str | None = None
    # Scorers run AFTER the agent finishes. 0 = pass. Stack-agnostic.
    test_cmd: str | None = None
    lint_cmd: str | None = None
    build_cmd: str | None = None

    def __post_init__(self):
        # task.id is interpolated raw into a worktree dir, a git branch (ab/<label>),
        # and an artifacts/<label> dir; a '/' or '..' from an untrusted remote
        # skillab.toml (see _clone_from_github -> load_config) would escape those
        # roots. Same charset rule as resolve_target's owner/repo guard.
        if ".." in self.id or not re.fullmatch(r"[A-Za-z0-9._-]+", self.id):
            raise ValueError(f"unsafe task id {self.id!r}: use only [A-Za-z0-9._-] "
                             f"(no path separators, no '..').")


@dataclass(frozen=True)
class ExperimentConfig:
    repo_path: Path                    # the source repo (a git repo)
    base_ref: str                      # commit/branch every worktree forks from
    skill_src: Path                    # the (primary) SKILL.md folder under test
    skill_name: str                    # installed as .claude/skills/<skill_name>/
    # Head-to-head: set skill_b_src/skill_b_name to compare TWO skills. Then the
    # experiment runs 3 arms -- skill A, skill B, and the no-skill control -- and
    # reports all pairwise comparisons (A vs control, B vs control, A vs B). Leave
    # unset for the classic 2-arm skill-on vs skill-off test.
    skill_b_src: Path | None = None
    skill_b_name: str | None = None
    # PINNED model id, never a drifting alias like "sonnet" (which would silently
    # change mid-experiment and confound the comparison).
    model: str = "claude-sonnet-4-6"
    # Per-arm MODEL overrides -> compare MODELS, not just skills (e.g. the same skill
    # under sonnet vs opus). None = use `model`. When arms differ by model, arm_label
    # disambiguates by model so the report shows e.g. "my-skill @ opus".
    model_a: str | None = None         # SKILL_ON arm
    model_b: str | None = None         # SKILL_B arm (head-to-head)
    model_off: str | None = None       # control arm
    # Drop the no-skill control for a pure A-vs-B head-to-head (skill or model) so a
    # 2-config comparison doesn't pay for a third arm.
    include_control: bool = True
    # Per-arm RUNNER override -> compare AGENTS (Claude vs Codex vs any CLI). None =
    # the built-in `claude` runner. A value is a curated runner name (e.g. "codex") or
    # a raw command template containing {prompt_file} (advanced; the prompt is passed
    # by FILE, never interpolated into the shell). A command arm runs no skill / no
    # activation and reports no cost (see plan 022). NOT settable via the serve API.
    runner_a: str | None = None
    runner_b: str | None = None
    runner_off: str | None = None
    k: int = 5                         # runs PER ARM PER TASK. k=1 proves nothing.
    max_concurrency: int = 4
    max_turns: int = 30
    agent_timeout_s: float = 1200.0    # wall-clock kill for a hung `claude -p`
    setup_timeout_s: float = 1200.0
    scorer_timeout_s: int = 600
    worktree_root: Path = Path("/tmp/skill_ab_worktrees")
    results_dir: Path = Path("/tmp/skill_ab_results")   # JSONL + failure artifacts
    permission_mode: str = "acceptEdits"   # use --dangerously-skip... only in a container
    # `Skill` MUST be present or the skill can never be invoked in headless -p.
    allowed_tools: tuple[str, ...] = ("Read", "Edit", "Write", "Bash", "Skill")
    # --disallowedTools deny rules (take precedence over allow). Used to forbid
    # remote-mutating commands so runs can't corrupt shared state / each other --
    # e.g. ("Bash(gh:*)", "Bash(git push:*)") for a PR target so no run can reply to
    # or resolve review threads on GitHub, or push.
    disallowed_tools: tuple[str, ...] = ()
    primary_metric: str = "tests_pass"     # pre-registered; excluded from BH correction
    keep_failed_worktrees: bool = False    # leave failed/invalid trees on disk for debugging
    # Isolation: how a skill is made active per arm.
    #  "worktree" (default) — install SKILL.md into the worktree's .claude/skills/
    #     and let auto-discovery invoke it (measures the skill as shipped).
    #  "inject" — run with --disable-slash-commands (suppress ALL ambient skills incl.
    #     globally-enabled plugins) and force-inject ONLY this arm's SKILL.md body via
    #     --append-system-prompt-file. Use when the skills under test live in a global
    #     plugin (else both would leak into every arm). `skill-test`/`quick` uses this.
    isolation: str = "worktree"
    # Robustness knobs
    preflight_repeats: int = 3         # runs of each check on clean base to detect flakiness
    bootstrap_iters: int = 10_000
    permutation_iters: int = 10_000
    bootstrap_alpha: float = 0.05
    # Qualitative blind judge (opt-in, NON-DETERMINISTIC, costs extra `claude` calls)
    capture_diffs: bool = True         # store each run's `git diff` (needed by the judge)
    judge_enabled: bool = False
    judge_model: str = "claude-sonnet-4-6"
    judge_axis: str = "overall code quality, correctness, and clarity"
    judge_max_pairs_per_task: int = 5  # ON×OFF diff pairs sampled per task
    judge_max_diff_chars: int = 20_000  # per-response truncation to bound the prompt
    # Budget / planning (see estimate_cost, the budget guard in run_experiment)
    cost_ceiling_usd: float | None = None   # abort remaining runs once total cost crosses this
    cost_per_run_usd: float | None = None   # dry-run cost prior (else measured from a pilot)

    def __post_init__(self):
        # Head-to-head needs BOTH skill_b fields. A half-set pair would otherwise
        # make a degenerate second "control" arm (label collision, control_vs_control).
        if (self.skill_b_src is None) != (self.skill_b_name is None):
            raise ValueError("skill_b_src and skill_b_name must be set together (3-arm "
                             "head-to-head) or both left unset (2-arm skill-on/off).")
        if self.skill_b_name is not None and self.skill_b_name == self.skill_name:
            # Same skill on both arms is allowed for a MODEL comparison (skill held
            # constant, models differ); only identical skill AND model = a true clash.
            if ((self.model_a or self.model) == (self.model_b or self.model)
                    and self.runner_a == self.runner_b):
                raise ValueError("skill_b must differ from skill_a unless the models "
                                 "or runners differ (identical arms otherwise).")
        # Arm LABELS are used as dict keys (manifest/summary/comparisons); they MUST be
        # injective or an arm is silently overwritten (e.g. two 'codex' runners).
        labels = [arm_label(self, a) for a in experiment_arms(self)]
        if len(set(labels)) != len(labels):
            raise ValueError(f"arm labels collide: {labels} -- give arms distinct "
                             "skills / models / runners.")


# ---------------------------------------------------------------------------
# Arms (generalised: each arm installs one skill, or nothing for the control)
# ---------------------------------------------------------------------------

def _is_head_to_head(cfg: ExperimentConfig) -> bool:
    # A second arm exists when skill_b is set OR a runner_b is set (a runner-only arm
    # B, e.g. codex with no skill -> 'claude+skill vs codex').
    return (cfg.skill_b_src is not None and cfg.skill_b_name is not None) \
        or cfg.runner_b is not None


def experiment_arms(cfg: ExperimentConfig) -> list[Arm]:
    """The arms in this experiment. 2-arm (skill vs control) by default; head-to-head
    (skill/model A, B, + control) when skill_b_src is set. include_control=False drops
    the no-skill control for a pure A-vs-B run."""
    if _is_head_to_head(cfg):
        return ([Arm.SKILL_ON, Arm.SKILL_B, Arm.SKILL_OFF] if cfg.include_control
                else [Arm.SKILL_ON, Arm.SKILL_B])
    return [Arm.SKILL_ON, Arm.SKILL_OFF]


def arm_skill(cfg: ExperimentConfig, arm: Arm) -> tuple[Path | None, str | None]:
    """(skill_src, skill_name) this arm installs; (None, None) for the control."""
    if arm is Arm.SKILL_ON:
        return cfg.skill_src, cfg.skill_name
    if arm is Arm.SKILL_B:
        return cfg.skill_b_src, cfg.skill_b_name
    return None, None


def arm_runner(cfg: ExperimentConfig, arm: Arm) -> str | None:
    """The runner override for this arm: None = the built-in `claude` runner; else a
    curated runner name ('codex') or a raw command template. The new agent axis."""
    if arm is Arm.SKILL_ON:
        return cfg.runner_a
    if arm is Arm.SKILL_B:
        return cfg.runner_b
    return cfg.runner_off


def _runner_label(runner: str | None) -> str:
    """Short label for a runner: 'claude' (built-in), a registry name, or the first
    token of a raw command template."""
    if runner is None:
        return "claude"
    if runner in _RUNNERS:
        return _RUNNERS[runner]["label"]
    return runner.split()[0] if runner.split() else "command"


def _runners_vary(cfg: ExperimentConfig) -> bool:
    """True when the arms don't all use the same runner (a cross-CLI comparison)."""
    return len({arm_runner(cfg, a) for a in experiment_arms(cfg)}) > 1


def _pair_is_cross_runner(cfg: ExperimentConfig, a: Arm, b: Arm) -> bool:
    """True when the two arms of a comparison run under different CLIs. Such a pair is
    confounded -- it bundles the agent binary, its default model, its prompt handling,
    and a SEPARATE login+billing with the skill effect -- so it can never earn a
    confident green verdict (plan 022, blocker #4)."""
    return arm_runner(cfg, a) != arm_runner(cfg, b)


def arm_model(cfg: ExperimentConfig, arm: Arm) -> str | None:
    """The CLAUDE model this arm runs under (per-arm override, else the default).
    None for a command arm -- the model axis is Claude-only (a codex arm has no
    claude model; see plan 022 'model axis isolation')."""
    if arm_runner(cfg, arm) is not None:
        return None
    if arm is Arm.SKILL_ON:
        return cfg.model_a or cfg.model
    if arm is Arm.SKILL_B:
        return cfg.model_b or cfg.model
    return cfg.model_off or cfg.model


def _short_model(model: str) -> str:
    """Compact label for a model id: 'claude-opus-4-8' -> 'opus'. Full id otherwise."""
    parts = model.split("-")
    return parts[1] if len(parts) >= 2 and parts[0] == "claude" else model


def _models_vary(cfg: ExperimentConfig) -> bool:
    """True when the CLAUDE arms don't all run the same model (a model comparison).
    Command arms are excluded -- they have no claude model."""
    models = {arm_model(cfg, a) for a in experiment_arms(cfg) if arm_runner(cfg, a) is None}
    return len(models) > 1


def arm_label(cfg: ExperimentConfig, arm: Arm) -> str:
    """Human label for an arm. A command arm is labelled by its RUNNER ('codex'). A
    claude arm is the skill name (or 'control'), plus the model when claude arms differ
    by model ('my-skill @ opus'). Backward-compatible when no runner/model varies."""
    if arm_runner(cfg, arm) is not None:
        return _runner_label(arm_runner(cfg, arm))
    _src, name = arm_skill(cfg, arm)
    base = name or "control"
    if _models_vary(cfg):
        return f"{base} @ {_short_model(arm_model(cfg, arm))}"
    return base


def all_skill_names(cfg: ExperimentConfig) -> set[str]:
    names = {cfg.skill_name}
    if _is_head_to_head(cfg) and cfg.skill_b_name:
        names.add(cfg.skill_b_name)
    return names


def experiment_pairs(cfg: ExperimentConfig) -> list[tuple[Arm, Arm]]:
    """Ordered (treatment, reference) pairs to estimate. 2-arm: skill vs control.
    3-arm: each skill vs control, then A vs B (the head-to-head)."""
    if _is_head_to_head(cfg):
        if cfg.include_control:
            return [(Arm.SKILL_ON, Arm.SKILL_OFF), (Arm.SKILL_B, Arm.SKILL_OFF),
                    (Arm.SKILL_ON, Arm.SKILL_B)]
        return [(Arm.SKILL_ON, Arm.SKILL_B)]          # pure A vs B, no control
    return [(Arm.SKILL_ON, Arm.SKILL_OFF)]


def primary_pair(cfg: ExperimentConfig) -> tuple[Arm, Arm]:
    """The headline comparison for the badge. Head-to-head -> A vs B; else the
    skill vs control."""
    return (Arm.SKILL_ON, Arm.SKILL_B) if _is_head_to_head(cfg) else (Arm.SKILL_ON, Arm.SKILL_OFF)


def pair_key(cfg: ExperimentConfig, a: Arm, b: Arm) -> str:
    return f"{arm_label(cfg, a)}_vs_{arm_label(cfg, b)}"


# ---------------------------------------------------------------------------
# Result records
# ---------------------------------------------------------------------------

@dataclass
class RunResult:
    task_id: str
    arm: Arm
    run_index: int
    worktree: Path
    skill_activated: bool | None       # detected on BOTH arms; None = couldn't tell
    activation_reason: str             # which rule fired / why (auditable)
    agent_ok: bool                     # clean, COMPLETE exit AND not is_error
    completed: bool = False            # stream reached a terminal `result` event
    timed_out: bool = False
    cost_usd: float | None = None
    num_turns: int | None = None
    hit_turn_limit: bool = False
    wall_seconds: float | None = None
    scores: dict[str, float] = field(default_factory=dict)
    error: str | None = None
    diff: str | None = None            # the agent's work product (for the blind judge)
    diff_truncated: bool = False       # True iff the captured diff was cut at
    #                                  # judge_max_diff_chars (renderer shows a marker)
    # skill_activated = did THIS arm's OWN intended skill fire (None for the
    # control / when there's nothing to detect). arm_skill_name = the arm's intended
    # skill (None = control). contaminated_by = a FOREIGN skill that fired in this
    # run (e.g. a leaked global ~/.claude install of the other arm's skill).
    arm_skill_name: str | None = None
    contaminated_by: str | None = None

    @property
    def contaminated(self) -> bool:
        """A skill the arm was NOT assigned fired in this run (treatment-as-assigned
        violated). For the control that's any skill; for a skill arm it's a
        different skill."""
        return self.contaminated_by is not None

    @property
    def itt_valid(self) -> bool:
        """Intention-to-treat validity = the PRIMARY estimand's inclusion rule.
        Keep every clean run; the ONLY exclusion is a contaminated run. Crucially
        we do NOT require the arm's skill to have fired: activation is a
        post-treatment outcome, and conditioning on it would bias the estimate."""
        if not self.agent_ok:
            return False
        return not self.contaminated

    @property
    def pp_valid(self) -> bool:
        """Per-protocol validity = the SECONDARY (potentially biased) estimand: a
        skill arm counts only if its skill fired; the control counts whenever it's
        clean (no skill fired -- already guaranteed by itt_valid)."""
        if not self.itt_valid:
            return False
        if self.arm_skill_name is None:        # control: complied iff not contaminated
            return True
        return self.skill_activated is True

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "arm": self.arm.value,
            "run_index": self.run_index,
            "worktree": str(self.worktree),
            "skill_activated": self.skill_activated,
            "activation_reason": self.activation_reason,
            "agent_ok": self.agent_ok,
            "completed": self.completed,
            "timed_out": self.timed_out,
            "contaminated": self.contaminated,
            "itt_valid": self.itt_valid,
            "pp_valid": self.pp_valid,
            "cost_usd": self.cost_usd,
            "num_turns": self.num_turns,
            "hit_turn_limit": self.hit_turn_limit,
            "wall_seconds": self.wall_seconds,
            "scores": self.scores,
            "error": self.error,
            "diff": self.diff,
            "diff_truncated": self.diff_truncated,
            "arm_skill_name": self.arm_skill_name,
            "contaminated_by": self.contaminated_by,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "RunResult":
        """Rebuild from a results.jsonl line. Derived keys (contaminated /
        itt_valid / pp_valid) are recomputed, not read."""
        return cls(
            task_id=d["task_id"], arm=Arm(d["arm"]), run_index=d["run_index"],
            worktree=Path(d["worktree"]), skill_activated=d["skill_activated"],
            activation_reason=d.get("activation_reason", ""), agent_ok=d["agent_ok"],
            completed=d.get("completed", False), timed_out=d.get("timed_out", False),
            cost_usd=d.get("cost_usd"), num_turns=d.get("num_turns"),
            hit_turn_limit=d.get("hit_turn_limit", False),
            wall_seconds=d.get("wall_seconds"), scores=dict(d.get("scores") or {}),
            error=d.get("error"), diff=d.get("diff"),
            diff_truncated=d.get("diff_truncated", False),
            arm_skill_name=d.get("arm_skill_name"), contaminated_by=d.get("contaminated_by"))


# ---------------------------------------------------------------------------
# Scorers (deterministic only -- the trustworthy signal)
# ---------------------------------------------------------------------------

class Scorer(Protocol):
    name: str
    direction: int                     # +1 bigger-better, -1 smaller-better
    def score(self, worktree: Path, task: Task) -> float: ...


def _run(cmd: str, cwd: Path, timeout: int = 600) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, shell=True, capture_output=True,
                          text=True, timeout=timeout)


@dataclass
class CommandScorer:
    """Pass/fail scorer that shells out. NaN => not applicable to this task."""
    name: str
    cmd_attr: str
    direction: int = 1
    timeout: int = 600

    def score(self, worktree: Path, task: Task) -> float:
        cmd = getattr(task, self.cmd_attr)
        if not cmd:
            return float("nan")
        try:
            return 1.0 if _run(cmd, worktree, self.timeout).returncode == 0 else 0.0
        except subprocess.TimeoutExpired:
            return 0.0


@dataclass
class DiffSizeScorer:
    name: str = "diff_lines"
    direction: int = -1
    timeout: int = 600

    def score(self, worktree: Path, task: Task) -> float:
        out = _run(f"git diff --numstat HEAD -- . {_SCRATCH_EXCLUDE}",
                   worktree, self.timeout).stdout
        total = 0
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) >= 2:
                for n in (parts[0], parts[1]):
                    if n.isdigit():
                        total += int(n)
        return float(total)


def default_scorers(cfg: ExperimentConfig | None = None) -> list[Scorer]:
    t = cfg.scorer_timeout_s if cfg else 600
    return [
        CommandScorer("tests_pass", "test_cmd", direction=1, timeout=t),
        CommandScorer("lint_pass", "lint_cmd", direction=1, timeout=t),
        CommandScorer("build_pass", "build_cmd", direction=1, timeout=t),
        DiffSizeScorer(timeout=t),
    ]


# ---------------------------------------------------------------------------
# git / worktree
# ---------------------------------------------------------------------------

def _git(repo: Path, args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          check=True, capture_output=True, text=True)


class Worktree:
    """`git worktree add` on enter, `--force` remove on exit -- unless `keep` is
    set (retain a failed/invalid tree for debugging)."""

    def __init__(self, cfg: ExperimentConfig, label: str):
        self.cfg = cfg
        self.label = label
        self.path = cfg.worktree_root / label
        self.branch = f"ab/{label}"
        self.keep = False

    def create(self) -> None:
        self.cfg.worktree_root.mkdir(parents=True, exist_ok=True)
        _git(self.cfg.repo_path,
             ["worktree", "add", "-b", self.branch, str(self.path), self.cfg.base_ref])

    def cleanup(self) -> None:
        if self.keep:
            return
        try:
            _git(self.cfg.repo_path, ["worktree", "remove", "--force", str(self.path)])
        except subprocess.CalledProcessError:
            shutil.rmtree(self.path, ignore_errors=True)
        try:
            _git(self.cfg.repo_path, ["branch", "-D", self.branch])
        except subprocess.CalledProcessError:
            pass

    # Sync context-manager form, used by the (synchronous) preflight. The async
    # runner calls create()/cleanup() via asyncio.to_thread so the blocking git
    # work doesn't freeze the event loop and stall sibling agents.
    def __enter__(self) -> "Worktree":
        self.create()
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()


# ---------------------------------------------------------------------------
# Skill toggle (the single independent variable)
# ---------------------------------------------------------------------------

def install_skill(worktree: Path, cfg: ExperimentConfig) -> None:
    """SKILL_ON convenience: install the primary skill. (setup_arm is the general
    per-arm form used by the runner.)"""
    _install_named_skill(worktree, cfg.skill_src, cfg.skill_name)


def _install_named_skill(worktree: Path, src: Path, name: str) -> None:
    """Drop a skill in so auto-discovery loads it. No --bare, which would also
    strip CLAUDE.md / other skills / MCP and confound the comparison. No canary."""
    dest = worktree / ".claude" / "skills" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, dirs_exist_ok=True)


def ensure_skill_absent(worktree: Path, cfg: ExperimentConfig) -> None:
    """SKILL_OFF convenience: remove the primary skill from the worktree."""
    shutil.rmtree(worktree / ".claude" / "skills" / cfg.skill_name, ignore_errors=True)


def setup_arm(worktree: Path, cfg: ExperimentConfig, arm: Arm) -> None:
    """Install ONLY this arm's intended skill (or nothing for the control), and
    remove every OTHER experiment skill so the arm runs exactly its treatment. A
    global ~/.claude install can still leak in -- which is why the detector runs on
    every arm and excludes runs where a FOREIGN skill fired (see detect_contamination)."""
    own_src, own_name = arm_skill(cfg, arm)
    for name in all_skill_names(cfg):
        if name and name != own_name:
            shutil.rmtree(worktree / ".claude" / "skills" / name, ignore_errors=True)
    if own_src and own_name:
        _install_named_skill(worktree, own_src, own_name)
    else:                                  # control: ensure even the primary is gone
        shutil.rmtree(worktree / ".claude" / "skills" / cfg.skill_name, ignore_errors=True)


def _find_skill_md(d: Path) -> Path | None:
    """The SKILL.md inside a skill dir, matched case-insensitively (some skills ship
    'Skill.md'); follows symlinks (many ~/.claude/skills entries are symlinks)."""
    try:
        if not d.is_dir():
            return None
        for f in d.iterdir():
            if f.is_file() and f.name.lower() == "skill.md":
                return f
    except OSError:
        return None
    return None


def resolve_skill(name: str, project_dir: Path | None = None) -> Path:
    """Resolve a skill NAME (or a path) to its SKILL.md file. Search order: project
    .claude/skills -> ~/.claude/skills -> plugin cache -> plugin marketplaces.
    Follows symlinks and is case-insensitive on the filename."""
    p = Path(name).expanduser()
    direct = _find_skill_md(p)
    if direct:
        return direct
    home = Path.home()
    roots: list[Path] = []
    if project_dir:
        roots.append(project_dir / ".claude" / "skills" / name)
    roots.append(home / ".claude" / "skills" / name)
    roots += sorted(home.glob(f".claude/plugins/cache/*/*/*/skills/{name}"))
    roots += sorted(home.glob(f".claude/plugins/marketplaces/*/plugins/*/skills/{name}"))
    for d in roots:
        md = _find_skill_md(d)
        if md:
            return md
    raise SystemExit(
        f"skill '{name}' not found. Looked in {project_dir or '.'}/.claude/skills, "
        f"~/.claude/skills, and ~/.claude/plugins/**. Pass a path to its folder instead.")


def _plugin_label(skill_dir: Path) -> str:
    """Best-effort plugin name from a skill dir. The plugin sits at a different depth
    for the two trees: marketplaces/<mkt>/plugins/<plugin>/skills/<name> (one above
    /skills) vs cache/<owner>/<repo>/<ver>/skills/<name> (use <repo>, not the version
    dir right above /skills)."""
    parts = skill_dir.parts
    try:
        if "cache" in parts:
            return skill_dir.parent.parent.parent.name or "plugin"   # <repo>
        return skill_dir.parent.parent.name or "plugin"              # <plugin>
    except (IndexError, AttributeError):
        return "plugin"


def list_available_skills(project_dir: Path | None = None,
                          home: Path | None = None) -> list[dict]:
    """Enumerate installed skills the SAME way resolve_skill searches, so a picker
    offers exactly what a typed name would resolve to. Dedup by name keeping the
    first hit in resolve precedence (project > ~/.claude > plugin cache >
    marketplaces); each entry is {name, source, path}. Best-effort (unreadable roots
    skipped); `home` is injectable for tests."""
    home = home or Path.home()
    found: dict[str, dict] = {}

    def add(d: Path, source: str) -> None:
        if d.name not in found and _find_skill_md(d):
            found[d.name] = {"name": d.name, "source": source, "path": str(d)}

    simple: list[tuple[Path, str]] = []
    if project_dir:
        simple.append((project_dir / ".claude" / "skills", "project"))
    simple.append((home / ".claude" / "skills", "global"))
    for root, source in simple:
        try:
            for d in sorted(root.iterdir()):
                add(d, source)
        except OSError:
            pass
    for d in sorted(home.glob(".claude/plugins/cache/*/*/*/skills/*")):
        add(d, "plugin: " + _plugin_label(d))
    for d in sorted(home.glob(".claude/plugins/marketplaces/*/plugins/*/skills/*")):
        add(d, "plugin: " + _plugin_label(d))
    return sorted(found.values(), key=lambda s: s["name"].lower())


def prepare_skill_guidance(skill_md: Path) -> str:
    """The static guidance body to inject as a system prompt: strip the YAML
    frontmatter and Claude Code `!`...`` dynamic-context blocks (which do NOT execute
    when injected via --append-system-prompt-file)."""
    text = skill_md.read_text()
    text = re.sub(r"^\s*---\n.*?\n---\n", "", text, count=1, flags=re.DOTALL)  # frontmatter
    # Strip Claude Code `!`cmd`` dynamic blocks ONLY at a token boundary and within a
    # line, so ordinary inline-code/backticks elsewhere aren't swallowed.
    text = re.sub(r"(?m)(^|\s)!`[^`\n]*`", r"\1", text)
    return text.strip()


def injected_system_prompt(skill_md: Path) -> str:
    return ("A skill is active for the task below. Follow its guidance:\n\n"
            + prepare_skill_guidance(skill_md))


def detect_contamination(events: list[dict], worktree: Path, cfg: ExperimentConfig,
                         own_name: str | None) -> str | None:
    """Did a skill this arm was NOT assigned fire? Returns the offending skill name
    or None. (A global install of the other arm's skill leaking in.)"""
    for name in all_skill_names(cfg):
        if name and name != own_name and detect_activation(events, worktree, name)[0] is True:
            return name
    return None


def _inject_leak(events: list[dict]) -> str | None:
    """Inject mode runs with --disable-slash-commands, so NO skill should fire. A
    `Skill` tool_use means one leaked past that -> the run is contaminated."""
    for ev in events:
        if ev.get("type") != "assistant":
            continue
        for b in ev.get("message", {}).get("content", []):
            if (isinstance(b, dict) and b.get("type") == "tool_use"
                    and (b.get("name") or "").lower() == "skill"):
                return "skill-leak"
    return None


def _tokens(cmd: str) -> list[str]:
    try:
        return shlex.split(cmd)
    except ValueError:
        return cmd.split()


def _resolves_to_local(token: str, worktree: Path, local_md_l: str) -> bool:
    """True iff `token`, interpreted relative to the worktree (the agent's cwd)
    and with ~ expanded, resolves to THIS worktree's own SKILL.md. A global
    ~/.claude/... or some other absolute path resolves elsewhere and is rejected,
    so a passive read of a global copy or a prompt echo cannot false-positive."""
    token = token.strip().strip("'\"")
    if not token or "skill.md" not in token.lower():
        return False
    try:
        p = Path(token).expanduser()
        if not p.is_absolute():
            p = worktree / p
        return str(p.resolve()).lower() == local_md_l
    except (OSError, ValueError, RuntimeError):
        return False


def detect_activation(events: list[dict], worktree: Path,
                      skill_name: str) -> tuple[bool | None, str]:
    """Did the skill actually fire? Returns (verdict, reason).

      verdict True  : a `Skill` tool_use whose name EXACTLY equals this skill, OR
                      a Read/Edit/Write/Bash that resolves to THIS worktree's own
                      SKILL.md (the body load that progressive disclosure triggers
                      on invocation).
      verdict False : the stream completed and no such signal appeared.
      verdict None  : no events at all, or the stream was truncated before a
                      terminal `result` event -> undeterminable (treated invalid
                      rather than guessed as a non-activation).

    Matching is against STRUCTURED tool inputs with exact name equality and
    path-resolved-into-worktree checks, so an incidental grep/cat, the prompt
    echoing the path, a global ~/.claude copy, or a substring-colliding skill
    name ('test' vs 'test-runner') cannot produce a false activation.
    """
    name_l = skill_name.strip().lower()
    local_md = worktree / ".claude" / "skills" / skill_name / "SKILL.md"
    try:
        local_md_l = str(local_md.resolve()).lower()
    except (OSError, RuntimeError):
        local_md_l = str(local_md).lower()

    saw_event = bool(events)
    completed = any(ev.get("type") == "result" for ev in events)

    for ev in events:
        if ev.get("type") != "assistant":
            continue
        for block in ev.get("message", {}).get("content", []):
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            tool = (block.get("name") or "").lower()
            inp = block.get("input", {}) or {}
            if not isinstance(inp, dict):
                continue
            # (1) explicit Skill tool -- EXACT name equality (no substring).
            if tool == "skill":
                called = (inp.get("name") or inp.get("skill")
                          or inp.get("command") or "")
                if str(called).strip().lower() == name_l:
                    return True, f"Skill tool invoked '{called}'"
            # (2) the skill's own LOCAL SKILL.md being read / edited.
            elif tool in ("read", "edit", "write"):
                fp = inp.get("file_path") or inp.get("path") or ""
                if fp and _resolves_to_local(str(fp), worktree, local_md_l):
                    return True, f"{tool} of local SKILL.md ({fp})"
            # (3) a Bash command that cat/heads/greps THIS worktree's SKILL.md.
            elif tool == "bash":
                for tok in _tokens(str(inp.get("command") or "")):
                    if _resolves_to_local(tok, worktree, local_md_l):
                        return True, f"Bash referenced local SKILL.md ({tok})"

    if not saw_event:
        return None, "no events in stream"
    if not completed:
        return None, "stream truncated (no result event)"
    return False, "no activation signal observed"


# ---------------------------------------------------------------------------
# Agent runner (claude -p headless)
# ---------------------------------------------------------------------------

async def _spawn_and_drain(argv: list[str], cwd: Path, timeout_s: float,
                           stdin_data: str | None = None
                           ) -> tuple[int | None, bytes, bytes, bool]:
    """Spawn `argv` in its OWN process group so a timeout can killpg the whole tree --
    agent CLIs (claude, codex, aider) spawn language-servers / MCP / watchers that
    would otherwise keep the stdout pipe open and hang the drain forever. Feeds
    optional stdin, returns (rc, stdout, stderr, timed_out) with a bounded post-kill
    drain. Shared by run_agent (claude) and run_command_agent (any CLI)."""
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd),
        stdin=(asyncio.subprocess.PIPE if stdin_data is not None else None),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    inp = stdin_data.encode() if stdin_data is not None else None
    timed_out = False
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(inp), timeout_s)
    except asyncio.TimeoutError:
        timed_out = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        try:                                   # bounded drain: don't hang the slot
            stdout, stderr = await asyncio.wait_for(proc.communicate(), 10.0)
        except (asyncio.TimeoutError, Exception):  # noqa: BLE001
            stdout, stderr = b"", b""
    return proc.returncode, stdout, stderr, timed_out


async def run_agent(worktree: Path, task: Task, cfg: ExperimentConfig,
                    inject_file: Path | None = None,
                    disable_skills: bool = False, model: str | None = None) -> dict:
    cmd = [
        "claude", "-p", task.prompt,
        "--output-format", "stream-json",
        "--verbose",                       # required to get the full event stream
        "--model", model or cfg.model,     # per-arm model override (sonnet vs opus)
        "--max-turns", str(cfg.max_turns),
        "--permission-mode", cfg.permission_mode,
        "--allowedTools", ",".join(cfg.allowed_tools),
        # In a container, replace the two permission lines above with:
        #   "--dangerously-skip-permissions",
    ]
    if cfg.disallowed_tools:               # deny rules win over allow (e.g. block gh/push)
        cmd += ["--disallowedTools", ",".join(cfg.disallowed_tools)]
    if disable_skills:                     # inject mode: suppress ambient skills
        # --disable-slash-commands empties slash_commands and removes the Skill tool
        # (verified suppressing globally-enabled PLUGIN skills); _inject_leak is the
        # backstop if one fires anyway.
        cmd.append("--disable-slash-commands")
    if inject_file is not None:            # ...and force-inject THIS arm's guidance
        cmd += ["--append-system-prompt-file", str(inject_file)]
    rc, stdout, stderr, timed_out = await _spawn_and_drain(
        cmd, worktree, cfg.agent_timeout_s)

    events: list[dict] = []
    for line in stdout.decode(errors="replace").splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                pass

    final = next((e for e in reversed(events) if e.get("type") == "result"), {})
    completed = bool(final)
    is_error = bool(final.get("is_error", False))
    # A truncated/killed run is NOT a clean non-activation: require a terminal
    # result event so half-finished runs are dropped, not silently scored.
    ok = rc == 0 and completed and not is_error and not timed_out
    return {
        "ok": ok,
        "events": events,
        "completed": completed,
        "timed_out": timed_out,
        "cost_usd": final.get("total_cost_usd"),
        "num_turns": final.get("num_turns"),
        "hit_turn_limit": final.get("subtype") == "error_max_turns",
        "stderr": stderr.decode(errors="replace"),
    }


async def run_command_agent(worktree: Path, task: Task, cfg: ExperimentConfig,
                            runner: str, timeout_s: float) -> dict:
    """Run a NON-claude agent CLI in the worktree. `runner` is a curated registry name
    ('codex') -> prompt fed via STDIN, or a raw command template with {prompt_file} ->
    prompt written to a temp file whose shell-quoted path is substituted (NEVER the
    prompt text -- injection-safe). Returns run_agent's dict shape minus Claude-only
    fields (events=[], cost_usd/num_turns=None). agent_ok is EXPOSURE-ONLY: it ran and
    didn't time out (rc-agnostic) -- a nonzero exit / empty diff is an OUTCOME captured
    by tests_pass/diff_lines, never censored (see plan 022)."""
    spec = _RUNNERS.get(runner)
    prompt_file: Path | None = None
    try:
        if spec is not None:                       # curated preset: prompt on stdin
            argv, stdin_data = list(spec["argv"]), task.prompt
        else:                                      # advanced: raw template + {prompt_file}
            fd, pf = tempfile.mkstemp(suffix=".txt", prefix="skillab_prompt_")
            os.close(fd)
            prompt_file = Path(pf)
            await asyncio.to_thread(prompt_file.write_text, task.prompt)
            cmdline = runner.replace("{prompt_file}", shlex.quote(str(prompt_file)))
            argv, stdin_data = ["/bin/sh", "-c", cmdline], None
        rc, out, err, timed_out = await _spawn_and_drain(argv, worktree, timeout_s, stdin_data)
    finally:
        if prompt_file is not None:
            prompt_file.unlink(missing_ok=True)
    return {
        "ok": (not timed_out) and rc is not None,  # exposure-only, rc-agnostic
        "events": [], "completed": not timed_out, "timed_out": timed_out,
        "cost_usd": None, "num_turns": None, "hit_turn_limit": False,
        "stderr": err.decode(errors="replace"),
        "transcript": out.decode(errors="replace"), "returncode": rc,
    }


# ---------------------------------------------------------------------------
# Flaky-test pre-flight
# ---------------------------------------------------------------------------

@dataclass
class Preflight:
    # (task_id, scorer_name) pairs that are unreliable and must be dropped
    quarantined: set[tuple[str, str]] = field(default_factory=set)
    notes: list[str] = field(default_factory=list)

    def keep(self, task_id: str, scorer_name: str) -> bool:
        return (task_id, scorer_name) not in self.quarantined


def run_preflight(cfg: ExperimentConfig, tasks: list[Task],
                  scorers: list[Scorer]) -> Preflight:
    """Run each task's checks N times on an UNTOUCHED base_ref worktree (with deps
    installed via setup_cmd first). One worktree per (task, repeat) so setup runs
    once per repeat, not once per scorer.
      * setup_cmd failing on the clean base -> quarantine the whole task, loudly
        (a misconfigured install would otherwise read as a phantom skill effect).
      * Nondeterministic across repeats -> quarantine (flaky).
      * Deterministic but failing pass/fail on the clean baseline -> quarantine
        (a perpetually-red suite measures nothing) and note it loudly."""
    pf = Preflight()
    pass_fail = {s.name for s in scorers if isinstance(s, CommandScorer)}

    for task in tasks:
        vals: dict[str, list[float]] = {s.name: [] for s in scorers}
        raised: set[str] = set()
        setup_failed = False

        for r in range(cfg.preflight_repeats):
            with Worktree(cfg, f"preflight-{task.id}-{r}") as wt:
                if task.setup_cmd:
                    try:
                        res = _run(task.setup_cmd, wt.path, cfg.setup_timeout_s)
                    except subprocess.TimeoutExpired:
                        setup_failed = True
                        pf.notes.append(
                            f"{task.id}: setup_cmd timed out on clean base; "
                            f"task quarantined (fix wiring)")
                        break
                    if res.returncode != 0:
                        setup_failed = True
                        pf.notes.append(
                            f"{task.id}: setup_cmd failed on clean base "
                            f"(rc={res.returncode}); task quarantined (fix wiring). "
                            f"stderr: {res.stderr.strip()[:200]}")
                        break
                for s in scorers:
                    if s.name in raised:
                        continue
                    try:
                        v = s.score(wt.path, task)
                    except Exception as e:  # noqa: BLE001
                        raised.add(s.name)
                        pf.quarantined.add((task.id, s.name))
                        pf.notes.append(
                            f"{task.id}/{s.name}: scorer raised ({e}); quarantined")
                        continue
                    if v == v:            # not NaN -> applies
                        vals[s.name].append(v)

        if setup_failed:
            for s in scorers:
                pf.quarantined.add((task.id, s.name))
            continue

        for s in scorers:
            if s.name in raised:
                continue
            vs = vals[s.name]
            if not vs:
                continue
            if len({round(v, 6) for v in vs}) > 1:
                pf.quarantined.add((task.id, s.name))
                pf.notes.append(
                    f"{task.id}/{s.name}: nondeterministic on clean base "
                    f"({vs}); quarantined as flaky")
            elif s.name in pass_fail and vs[0] == 0.0:
                pf.quarantined.add((task.id, s.name))
                pf.notes.append(
                    f"{task.id}/{s.name}: already failing on clean base; "
                    f"quarantined (fix baseline first)")
    return pf


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------

def _dump_artifacts(wt_path: Path, label: str, events: list[dict],
                    stderr: str, result: RunResult, cfg: ExperimentConfig) -> None:
    """Persist a small debugging bundle for a failed/invalid run BEFORE the
    worktree is torn down: the event stream, the git diff, stderr, the record."""
    try:
        dest = cfg.results_dir / "artifacts" / label
        dest.mkdir(parents=True, exist_ok=True)
        with (dest / "events.jsonl").open("w") as f:
            for ev in events:
                f.write(json.dumps(ev) + "\n")
        (dest / "stderr.txt").write_text(stderr or "")
        (dest / "result.json").write_text(json.dumps(result.to_dict(), indent=2))
        try:
            diff = _run("git diff HEAD", wt_path).stdout
            (dest / "diff.txt").write_text(diff)
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass


def _failed_result(task: Task, arm: Arm, idx: int, cfg: ExperimentConfig,
                   reason: str, error: str) -> RunResult:
    return RunResult(
        task_id=task.id, arm=arm, run_index=idx,
        worktree=cfg.worktree_root / f"{task.id}-{arm.value}-{idx}",
        skill_activated=None, activation_reason=reason, agent_ok=False, error=error)


async def execute_run(task: Task, arm: Arm, idx: int, cfg: ExperimentConfig,
                      scorers: list[Scorer], pf: Preflight,
                      sem: asyncio.Semaphore, budget: dict | None = None,
                      on_event=None) -> RunResult:
    label = f"{task.id}-{arm.value}-{idx}"
    async with sem:
        # Budget gate INSIDE the semaphore: queued runs see stop only after they
        # acquire a slot, so the ceiling actually bounds spend (checking before the
        # semaphore is a no-op — gather steps every coroutine past it at once).
        if budget is not None and budget["stop"]:
            _emit(on_event, {"type": "run_skipped", "label": label,
                             "reason": "cost ceiling reached"})
            return _failed_result(task, arm, idx, cfg, "budget ceiling reached", _SKIPPED_ERR)
        _emit(on_event, {"type": "run_start", "label": label})
        wt = Worktree(cfg, label)
        try:
            await asyncio.to_thread(wt.create)   # blocking git, offloaded
        except Exception as e:  # noqa: BLE001
            return _failed_result(task, arm, idx, cfg, "worktree create failed", str(e))
        try:
            # All blocking subprocess / filesystem work below is offloaded via
            # asyncio.to_thread so it doesn't freeze the event loop and stall the
            # other concurrent agents' streaming I/O.
            own_src, own_name = arm_skill(cfg, arm)
            runner = arm_runner(cfg, arm)        # None = claude; else an external CLI
            disable_skills = cfg.isolation == "inject"
            if not disable_skills and runner is None:
                # Worktree mode: install ONLY this arm's skill (or nothing for control).
                # A command (external-CLI) arm never installs a Claude skill.
                await asyncio.to_thread(setup_arm, wt.path, cfg, arm)

            # Install deps etc. before the agent (a fresh worktree has none).
            if task.setup_cmd:
                try:
                    setup = await asyncio.to_thread(
                        _run, task.setup_cmd, wt.path, int(cfg.setup_timeout_s))
                except subprocess.TimeoutExpired:
                    wt.keep = cfg.keep_failed_worktrees
                    return _failed_result(task, arm, idx, cfg,
                                          "setup timed out", "setup_cmd timed out")
                if setup.returncode != 0:
                    wt.keep = cfg.keep_failed_worktrees
                    return _failed_result(
                        task, arm, idx, cfg, "setup failed",
                        f"setup_cmd rc={setup.returncode}: {setup.stderr[:500]}")

            # Inject mode: write the arm's guidance to a temp file OUTSIDE the worktree
            # (so it never pollutes the diff) and pass it via --append-system-prompt-file.
            # Command arms get no inject (they don't run claude).
            inject_file: Path | None = None
            if disable_skills and own_name and runner is None:
                md = (own_src if own_src and own_src.is_file()
                      else (_find_skill_md(own_src) if own_src else None))
                if md is None:                 # a skill arm with no resolvable SKILL.md is
                    wt.keep = cfg.keep_failed_worktrees   # NOT a silent no-skill run
                    return _failed_result(task, arm, idx, cfg, "inject: SKILL.md not found",
                                          f"no SKILL.md for skill '{own_name}' under {own_src}")
                fd, ip = tempfile.mkstemp(suffix=".md", prefix="skillab_inject_")
                os.close(fd)
                inject_file = Path(ip)
                try:
                    await asyncio.to_thread(inject_file.write_text, injected_system_prompt(md))
                except Exception as e:  # noqa: BLE001
                    inject_file.unlink(missing_ok=True)
                    wt.keep = cfg.keep_failed_worktrees
                    return _failed_result(task, arm, idx, cfg, "inject: guidance write failed",
                                          str(e))

            t0 = time.monotonic()
            if runner is None:
                try:
                    agent = await run_agent(wt.path, task, cfg, inject_file=inject_file,
                                            disable_skills=disable_skills,
                                            model=arm_model(cfg, arm))
                finally:
                    if inject_file is not None:
                        inject_file.unlink(missing_ok=True)
            else:                             # external CLI (codex / aider / raw command)
                agent = await run_command_agent(wt.path, task, cfg, runner,
                                                cfg.agent_timeout_s)
            wall = time.monotonic() - t0
            if on_event is not None:
                if runner is None:            # claude: replay the stream-json events
                    for ev in agent.get("events", []):
                        for out in _summarize_agent_event(ev, label):
                            _emit(on_event, out)
                else:                         # command: one transcript-tail event
                    tail = (agent.get("transcript") or "").strip()[-2000:]
                    _emit(on_event, {"type": "agent", "label": label, "kind": "text",
                                     "text": tail or "(no output)"})

            if runner is not None:
                # An external CLI runs no Claude skill -> no activation / contamination
                # concept. It is, in validity terms, a no-skill arm whose agent is the
                # external CLI. cost/turns are None (set by run_command_agent).
                activated = None
                reason = f"{_runner_label(runner)}: external CLI (no skill / no activation)"
                contaminated_by = None
            elif disable_skills:
                # Guidance is injected by construction -> activation True ONLY when an
                # inject file was actually produced (control arm has none). Contamination
                # = a Skill tool_use fired despite --disable-slash-commands.
                activated = True if inject_file is not None else None
                contaminated_by = _inject_leak(agent["events"])
                reason = (f"inject: '{own_name}' guidance force-injected" if own_name
                          else "inject: control (no skill)")
                if contaminated_by:
                    reason += "; CONTAMINATED (a skill fired despite --disable-slash-commands)"
            elif own_name:
                activated, reason = detect_activation(agent["events"], wt.path, own_name)
                contaminated_by = detect_contamination(agent["events"], wt.path, cfg, own_name)
                if contaminated_by:
                    reason = f"{reason}; CONTAMINATED by '{contaminated_by}'"
            else:
                activated, reason = None, "control: no skill assigned"
                contaminated_by = detect_contamination(agent["events"], wt.path, cfg, own_name)
                if contaminated_by:
                    reason = f"{reason}; CONTAMINATED by '{contaminated_by}'"
            result = RunResult(
                task_id=task.id, arm=arm, run_index=idx, worktree=wt.path,
                skill_activated=activated, activation_reason=reason,
                arm_skill_name=own_name, contaminated_by=contaminated_by,
                agent_ok=agent["ok"], completed=agent["completed"],
                timed_out=agent["timed_out"], cost_usd=agent["cost_usd"],
                num_turns=agent["num_turns"], hit_turn_limit=agent["hit_turn_limit"],
                wall_seconds=wall,
                error=(agent["stderr"] or None) if not agent["ok"] else None,
            )
            # Make the agent's NEW (untracked) files visible to `git diff` -- a
            # coding task often ADDS files, which `git diff HEAD` alone misses,
            # emptying both the diff_lines metric and the judged work product.
            # Exclude `.claude` so the installed skill can never leak into the
            # (blind) judged diff.
            try:
                await asyncio.to_thread(
                    _run, f"git add -N -- . {_SCRATCH_EXCLUDE}", wt.path, 120)
            except Exception:  # noqa: BLE001
                pass
            for s in scorers:
                if not pf.keep(task.id, s.name):
                    continue
                try:
                    v = await asyncio.to_thread(s.score, wt.path, task)
                    if v == v:
                        result.scores[s.name] = v
                except Exception as e:  # noqa: BLE001
                    result.error = f"{s.name} scorer failed: {e}"
            if result.cost_usd is not None:
                result.scores["cost_usd"] = result.cost_usd

            # Capture the work product (the diff, minus the skill dir) before
            # teardown -- the blind judge reads it, and it makes the JSONL fully
            # reproducible offline.
            if cfg.capture_diffs:
                try:
                    d = await asyncio.to_thread(
                        _run, f"git diff HEAD -- . {_SCRATCH_EXCLUDE}", wt.path, 120)
                    result.diff = d.stdout[:cfg.judge_max_diff_chars]
                    result.diff_truncated = len(d.stdout) >= cfg.judge_max_diff_chars
                except Exception:  # noqa: BLE001
                    pass

            if (not result.agent_ok) or (not result.itt_valid):
                await asyncio.to_thread(_dump_artifacts, wt.path, label,
                                        agent["events"], agent.get("stderr", ""),
                                        result, cfg)
                wt.keep = cfg.keep_failed_worktrees
            return result
        except Exception as e:  # noqa: BLE001
            # Never let one run abort the whole gather (conc-1 residual). Retain
            # the tree for debugging if the user asked to keep failures.
            wt.keep = cfg.keep_failed_worktrees
            return _failed_result(task, arm, idx, cfg, f"run raised: {e}", str(e))
        finally:
            try:
                await asyncio.to_thread(wt.cleanup)   # respects wt.keep
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

_SKIPPED_ERR = "skipped: cost_ceiling_usd reached"


def _dedupe_runs(prior: list[RunResult], fresh: list[RunResult]) -> list[RunResult]:
    """Collapse to the LAST record per (task_id, arm, run_index). On resume a
    failed/contaminated cell is retried, so the SAME cell can appear in both
    `prior` (the superseded attempt) and `fresh` (the retry); returning both would
    double-count it in raw denominators. `fresh` wins over `prior`; a cell seen
    only once passes through unchanged. Order is stable: a cell keeps the position
    where it was first seen (dict preserves first-insertion order on overwrite)."""
    by_cell: dict[tuple[str, str, int], RunResult] = {}
    for r in (*prior, *fresh):     # prior first; a later fresh retry overwrites it
        by_cell[(r.task_id, r.arm.value, r.run_index)] = r
    return list(by_cell.values())


def _emit(on_event, evt: dict) -> None:
    """Best-effort progress emission. A UI subscriber dying must NEVER raise into a
    paid run, so every callback is swallowed."""
    if on_event is None:
        return
    try:
        on_event(evt)
    except Exception:  # noqa: BLE001
        pass


def _summarize_agent_event(ev: dict, label: str) -> list[dict]:
    """Map one `claude -p` stream-json event to UI `agent` events (text/tool/result).
    Tolerant of CLI-version shape drift: unknown shapes yield nothing."""
    out: list[dict] = []
    t = ev.get("type")
    if t == "assistant":
        for blk in ((ev.get("message") or {}).get("content") or []):
            bt = blk.get("type")
            if bt == "text" and (blk.get("text") or "").strip():
                out.append({"type": "agent", "label": label, "kind": "text",
                            "text": blk["text"][:2000]})
            elif bt == "tool_use":
                out.append({"type": "agent", "label": label, "kind": "tool",
                            "tool": blk.get("name", "?")})
    elif t == "result":
        out.append({"type": "agent", "label": label, "kind": "result",
                    "cost_usd": ev.get("total_cost_usd"), "turns": ev.get("num_turns")})
    return out


async def run_experiment(cfg: ExperimentConfig, tasks: list[Task],
                         scorers: list[Scorer] | None = None,
                         resume: bool = False, on_event=None
                         ) -> tuple[list[RunResult], Preflight]:
    scorers = scorers or default_scorers(cfg)
    pf = run_preflight(cfg, tasks, scorers)
    sem = asyncio.Semaphore(cfg.max_concurrency)

    cfg.results_dir.mkdir(parents=True, exist_ok=True)
    results_path = cfg.results_dir / "results.jsonl"
    manifest_path = cfg.results_dir / "manifest.json"
    manifest = experiment_manifest(cfg, seed=0, timestamp=time.time())

    prior: list[RunResult] = []
    done: set[tuple[str, str, int]] = set()
    if resume and results_path.exists():
        _check_resume_compatible(manifest_path, manifest)
        prior = load_results(results_path)
        # Only SUCCESSFUL cells count as done -- failed/timed-out cells are kept on
        # disk for the audit trail but must be retried, not silently skipped.
        done = {(r.task_id, r.arm.value, r.run_index) for r in prior if r.agent_ok}
    else:
        results_path.write_text("")          # truncate prior run
    manifest_path.write_text(json.dumps(manifest, indent=2))

    write_lock = asyncio.Lock()
    spent0 = sum(r.cost_usd or 0.0 for r in prior)
    # Pre-trip the ceiling: a resume already over budget must spend nothing more.
    budget = {"spent": spent0, "stop": _should_stop(spent0, cfg.cost_ceiling_usd)}

    async def run_and_persist(task: Task, arm: Arm, i: int) -> RunResult:
        res = await execute_run(task, arm, i, cfg, scorers, pf, sem, budget, on_event)
        if res.error == _SKIPPED_ERR:         # ceiling skip -> don't persist or count
            return res
        # Persistence is best-effort: a write failure must NOT delete an
        # already-computed (paid) run from the in-memory analysis set.
        try:
            async with write_lock:        # persist each result as it lands
                budget["spent"] += res.cost_usd or 0.0
                if _should_stop(budget["spent"], cfg.cost_ceiling_usd):
                    budget["stop"] = True
                with results_path.open("a") as f:
                    f.write(json.dumps(res.to_dict()) + "\n")
        except Exception as e:  # noqa: BLE001
            res.error = f"{res.error or ''} | persist failed: {e}".strip(" |")
        _emit(on_event, {"type": "run_done", "label": f"{task.id}-{arm.value}-{i}",
                         "itt_valid": res.itt_valid, "activated": res.skill_activated,
                         "contaminated_by": res.contaminated_by, "cost_usd": res.cost_usd,
                         "turns": res.num_turns, "diff_lines": res.scores.get("diff_lines")})
        _emit(on_event, {"type": "cost", "spent_usd": budget["spent"],
                         "ceiling_usd": cfg.cost_ceiling_usd})
        return res

    cells = [(task, arm, i)
             for task in tasks
             for arm in experiment_arms(cfg)
             for i in range(cfg.k)
             if (task.id, arm.value, i) not in done]
    _emit(on_event, {"type": "experiment_start",
                     "arms": [arm_label(cfg, a) for a in experiment_arms(cfg)],
                     "cells": [{"label": f"{t.id}-{a.value}-{i}", "task": t.id,
                                "arm": arm_label(cfg, a), "idx": i} for t, a, i in cells]})
    jobs = [run_and_persist(task, arm, i) for task, arm, i in cells]
    raw = await asyncio.gather(*jobs, return_exceptions=True)
    fresh = [r for r in raw if isinstance(r, RunResult) and r.error != _SKIPPED_ERR]
    deduped = _dedupe_runs(prior, fresh)
    # Rewrite results.jsonl from the deduped set so disk matches the in-memory
    # analysis list and a repeatedly-retried cell stops growing the file. Safe
    # after gather (no concurrent writers); from_dict recomputes derived keys, so a
    # rewritten line round-trips identically. The superseded failed attempt's deep
    # audit trail (events/diff/stderr) is still under results_dir/artifacts/<label>/.
    with results_path.open("w") as f:
        for r in deduped:
            f.write(json.dumps(r.to_dict()) + "\n")
    return deduped, pf


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

@dataclass
class DiffEstimate:
    metric: str
    mean_on: float
    mean_off: float
    point: float          # mean_on - mean_off, over the SHARED task set
    ci_low: float
    ci_high: float
    p_value: float        # cluster permutation, null-calibrated
    n_on: int
    n_off: int
    n_tasks: int          # shared tasks (the basis for point, CI AND p)
    clustered: bool       # False => degenerate flat fallback (anticonservative)
    q_value: float | None = None   # BH-adjusted, secondary metrics only


def _grouped(results: list[RunResult], arm: Arm, metric: str,
             mode: str) -> dict[str, list[float]]:
    """metric values grouped by task, for the given arm, under inclusion `mode`
    ('itt' or 'pp')."""
    g: dict[str, list[float]] = {}
    for r in results:
        ok = r.itt_valid if mode == "itt" else r.pp_valid
        if r.arm is arm and ok and metric in r.scores:
            g.setdefault(r.task_id, []).append(r.scores[metric])
    return g


def cluster_bootstrap_ci(on: dict[str, list[float]], off: dict[str, list[float]],
                         shared: list[str], iters: int, alpha: float,
                         rng: random.Random) -> tuple[float, float, bool]:
    """Resample TASKS with replacement, then runs within each sampled task (per
    arm). The shared task `picks` are drawn ONCE per iteration and used for BOTH
    arms, so between-task variance cancels in the difference (unpaired,
    task-clustered). Falls back to flat run-level resampling when <2 shared
    tasks; that path is anticonservative, flagged via the returned bool."""
    diffs: list[float] = []
    clustered = len(shared) >= 2

    if not clustered:                                # degenerate clustering
        flat_on = [v for t in shared for v in on[t]]
        flat_off = [v for t in shared for v in off[t]]
        if not flat_on or not flat_off:
            return (0.0, 0.0, False)
        for _ in range(iters):
            ro = [rng.choice(flat_on) for _ in flat_on]
            rf = [rng.choice(flat_off) for _ in flat_off]
            diffs.append(statistics.fmean(ro) - statistics.fmean(rf))
    else:
        for _ in range(iters):
            picks = [rng.choice(shared) for _ in shared]
            on_vals, off_vals = [], []
            for t in picks:
                on_vals += [rng.choice(on[t]) for _ in on[t]]
                off_vals += [rng.choice(off[t]) for _ in off[t]]
            diffs.append(statistics.fmean(on_vals) - statistics.fmean(off_vals))

    diffs.sort()
    lo = diffs[int((alpha / 2) * len(diffs))]
    hi = diffs[min(len(diffs) - 1, int((1 - alpha / 2) * len(diffs)))]
    return (lo, hi, clustered)


def cluster_permutation_p(on: dict[str, list[float]], off: dict[str, list[float]],
                          shared: list[str], observed: float, iters: int,
                          rng: random.Random) -> float:
    """Null-calibrated two-sided p-value via a cluster-respecting permutation
    test: within each task, pool its ON+OFF run values and randomly reassign arm
    labels (exchangeability under the sharp null of no arm effect), recompute the
    pooled-mean difference. p = (#|perm| >= |observed| + 1) / (iters + 1).

    This fixes the all-ties degenerate case: when both arms are constant the
    observed diff is 0 and every permutation is also 0, so p -> 1.0 (no effect),
    not 0.0. And it never returns exactly 0 for a real effect."""
    obs = abs(observed)
    counts = [(list(on[t]), list(off[t]), len(on[t])) for t in shared]
    ge = 0
    for _ in range(iters):
        son = non = soff = noff = 0.0
        for ov, fv, k in counts:
            pooled = ov + fv
            rng.shuffle(pooled)
            a, b = pooled[:k], pooled[k:]
            son += sum(a)
            non += len(a)
            soff += sum(b)
            noff += len(b)
        if non and noff and abs(son / non - soff / noff) >= obs - 1e-12:
            ge += 1
    return (ge + 1) / (iters + 1)


def benjamini_hochberg(pvals: list[float]) -> list[float]:
    """BH step-up adjusted q-values, order preserved."""
    m = len(pvals)
    if m == 0:
        return []
    order = sorted(range(m), key=lambda i: pvals[i])
    q = [1.0] * m
    running = 1.0
    for rank in range(m, 0, -1):                     # largest p first
        i = order[rank - 1]
        running = min(running, pvals[i] * m / rank)
        q[i] = min(running, 1.0)
    return q


def estimate_diff(results: list[RunResult], metric: str, mode: str,
                  cfg: ExperimentConfig, rng: random.Random,
                  arm_a: Arm = Arm.SKILL_ON, arm_b: Arm = Arm.SKILL_OFF) -> DiffEstimate | None:
    """Point estimate, CI and p ALL computed over the SAME shared-task set, with
    the same pooled-mean estimand the bootstrap resamples -- so the delta always
    lies in a self-consistent universe with its interval. Defaults to the classic
    skill-on vs skill-off; pass arm_a/arm_b for any pairwise comparison."""
    on = _grouped(results, arm_a, metric, mode)
    off = _grouped(results, arm_b, metric, mode)
    shared = [t for t in on if t in off and on[t] and off[t]]
    if not shared:
        return None
    on_flat = [v for t in shared for v in on[t]]
    off_flat = [v for t in shared for v in off[t]]
    m_on, m_off = statistics.fmean(on_flat), statistics.fmean(off_flat)
    point = m_on - m_off
    lo, hi, clustered = cluster_bootstrap_ci(
        on, off, shared, cfg.bootstrap_iters, cfg.bootstrap_alpha, rng)
    p = cluster_permutation_p(on, off, shared, point, cfg.permutation_iters, rng)
    return DiffEstimate(metric, m_on, m_off, point, lo, hi, p,
                        len(on_flat), len(off_flat), len(shared), clustered)


def activation_rate(results: list[RunResult], arm: Arm) -> tuple[int, int]:
    """(fired, clean) among clean (agent_ok) runs of an arm. A first-stage /
    compliance diagnostic -- NOT an inclusion gate for the ITT estimate."""
    clean = [r for r in results if r.arm is arm and r.agent_ok]
    fired = sum(1 for r in clean if r.skill_activated is True)
    return fired, len(clean)


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def _row(est: DiffEstimate, dir_map: dict[str, int], alpha: float,
         use_q: bool) -> str:
    arrow = "↑good" if dir_map.get(est.metric, 1) > 0 else "↓good"
    flags = ""
    if not est.clustered:
        flags += "†"
    if est.n_tasks < 2:
        flags += "‡"
    if use_q and est.q_value is not None:
        sig = " *" if est.q_value < alpha else ""
        qp = f"{est.q_value:.3f}"
    else:
        sig = " *" if not (est.ci_low <= 0 <= est.ci_high) else ""
        qp = "—"
    return (f"| {est.metric}{flags} | {est.mean_on:.3f} | {est.mean_off:.3f} "
            f"| {est.point:+.3f}{sig} | [{est.ci_low:+.3f}, {est.ci_high:+.3f}] "
            f"| {est.p_value:.3f} | {qp} | {est.n_tasks} | {arrow} |")


def compute_estimates(results: list[RunResult], cfg: ExperimentConfig,
                      metrics: list[str], seed: int) -> dict:
    """Single source of truth for every pairwise estimate. Runs estimate_diff
    EXACTLY once per (pair, mode, metric) against ONE seeded RNG so the markdown,
    JSON and HTML renderers (and the badge) all read identical CIs/p-values --
    otherwise each renderer's own RNG, consumed in a different order, produces
    divergent CIs for the same run. Returns {pair_key: {"a", "b",
    "itt": {metric: DiffEstimate|None}, "pp": {metric: DiffEstimate|None}}}. BH
    q-values are applied to each pair's ITT secondary metrics (primary stays raw).
    Canonical RNG order: per pair, all ITT metrics then all PP metrics."""
    rng = random.Random(seed)
    out: dict = {}
    for a, b in experiment_pairs(cfg):
        itt = {m: estimate_diff(results, m, "itt", cfg, rng, a, b) for m in metrics}
        sec = [m for m in metrics if m != cfg.primary_metric and itt[m]]
        for m, q in zip(sec, benjamini_hochberg([itt[m].p_value for m in sec])):
            itt[m].q_value = q
        pp = {m: estimate_diff(results, m, "pp", cfg, rng, a, b) for m in metrics}
        out[pair_key(cfg, a, b)] = {
            "a": arm_label(cfg, a), "b": arm_label(cfg, b), "itt": itt, "pp": pp}
    return out


def _estimate_fields(e: DiffEstimate) -> dict:
    """The portable per-metric field dict the summary JSON has always emitted."""
    return {k: getattr(e, k) for k in (
        "mean_on", "mean_off", "point", "ci_low", "ci_high",
        "p_value", "q_value", "n_on", "n_off", "n_tasks", "clustered")}


def build_report(results: list[RunResult], pf: Preflight, cfg: ExperimentConfig,
                 scorers: list[Scorer] | None = None, seed: int = 0,
                 manifest: dict | None = None) -> str:
    scorers = scorers or default_scorers(cfg)
    if manifest is None:
        manifest = experiment_manifest(cfg, seed=seed)
    metrics = [s.name for s in scorers] + ["cost_usd"]
    ests = compute_estimates(results, cfg, metrics, seed)   # one shared pass
    dir_map = {s.name: s.direction for s in scorers}
    dir_map["cost_usd"] = -1
    alpha = cfg.bootstrap_alpha
    primary = cfg.primary_metric
    pairs = experiment_pairs(cfg)
    title_skill = cfg.skill_name if len(pairs) == 1 else f"{cfg.skill_name} vs {cfg.skill_b_name}"

    L = [
        f"# Skill A/B report — `{title_skill}`",
        f"model: {cfg.model} | k: {cfg.k}/arm/task | bootstrap: {cfg.bootstrap_iters:,} "
        f"| permutation: {cfg.permutation_iters:,} | arms: {len(experiment_arms(cfg))} "
        f"| total runs: {len(results)}",
        "",
        "<details><summary>reproducibility manifest</summary>",
        "",
        f"- harness {manifest['harness_version']} | claude CLI "
        f"{manifest.get('claude_cli_version') or '?'} | model {manifest['model']}",
        f"- repo {manifest['repo_path']} @ {manifest['base_ref']} "
        f"({(manifest.get('base_ref_sha') or '?')[:12]})",
        f"- SKILL.md sha256 {(manifest.get('skill_md_sha256') or '?')[:16]} | "
        f"seed {manifest['seed']} | {manifest.get('platform')}",
        "</details>",
        "",
        "## Validity & compliance (intention-to-treat)",
    ]
    for arm in experiment_arms(cfg):
        label = arm_label(cfg, arm)
        tot = sum(1 for r in results if r.arm is arm)
        val = sum(1 for r in results if r.arm is arm and r.itt_valid)
        fired, clean = activation_rate(results, arm)
        contam = sum(1 for r in results if r.arm is arm and r.contaminated)
        _src, name = arm_skill(cfg, arm)
        extra = ""
        if name:
            act = f"{(100 * fired / clean):.0f}%" if clean else "n/a"
            extra = f" | activation {fired}/{clean} = {act}"
        L.append(f"- **{label}**: {val}/{tot} valid runs{extra}")
        if contam:
            L.append(f"  - ⚠️ {contam} run(s) contaminated — a foreign skill fired "
                     f"(global ~/.claude install?). Excluded; fix before trusting deltas.")
        if name and clean and fired == 0:
            L.append(f"  - ⚠️ '{name}' NEVER fired on a clean run. Confirm the activation "
                     f"fingerprint and that `Skill` is in allowed_tools.")

    if len({r.task_id for r in results}) < 2:
        L.append("")
        L.append("> ⚠️ **Single task** — the cluster stats need ≥2 tasks, so the badge "
                 "stays *inconclusive* by design. Treat this as suggestive, not a verdict; "
                 "add more tasks/PRs (or raise k) for a significant result.")

    if pf.notes:
        L += ["", "## Pre-flight (quarantined checks)"]
        L += [f"- {n}" for n in pf.notes]

    if len(pairs) > 1:
        L += ["", f"_{len(pairs)} comparisons below (each skill vs control, then the "
              f"head-to-head). Keep the comparison family in mind — significance is "
              f"BH-corrected WITHIN each comparison's secondary metrics, not across them._"]

    for a, b in pairs:
        la, lb = arm_label(cfg, a), arm_label(cfg, b)
        pe = ests[pair_key(cfg, a, b)]          # precomputed: BH q already applied
        ph = (f"| metric | {la} | {lb} | delta | 95% CI | p | q | tasks | dir |\n"
              "|---|---|---|---|---|---|---|---|---|")
        L += ["", f"## {la} − {lb} (ITT)",
              f"_primary endpoint `{primary}` (pre-registered, uncorrected); secondary "
              f"metrics Benjamini–Hochberg corrected_", ph]
        est = pe["itt"][primary]
        L.append(_row(est, dir_map, alpha, use_q=False) if est
                 else f"| {primary} | — | — | (no shared tasks with valid runs) | | | | |")
        for m in metrics:
            if m == primary or not pe["itt"][m]:
                continue
            L.append(_row(pe["itt"][m], dir_map, alpha, use_q=True))
        pp_rows = [_row(e, dir_map, alpha, use_q=False) for m in metrics
                   if (e := pe["pp"][m])]
        if pp_rows:
            L += ["", "<details><summary>per-protocol (conditioned on activation — "
                  "biased)</summary>", "", ph] + pp_rows + ["</details>"]

    L += [
        "",
        "`*` = significant (primary/per-protocol: 95% CI excludes 0; secondary: "
        "BH q < α). `delta` = (left arm − right arm). `p` is a cluster permutation "
        "test (null-calibrated); the CI is a cluster bootstrap. `†` = <2 shared tasks "
        "so the CI used flat resampling (anticonservative). `‡` = <2 shared tasks. "
        "Read direction against `dir`: a positive delta on a `↓good` metric (cost, "
        "diff_lines) is *worse*.",
    ]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Qualitative blind judge (opt-in, NON-DETERMINISTIC)
# ---------------------------------------------------------------------------
#
# An LLM judge compares the ON-arm and OFF-arm WORK PRODUCTS (git diffs) for the
# same task on a qualitative axis the deterministic scorers can't capture. To stay
# honest about a noisy, non-deterministic measurement:
#   * BLIND: arm labels are stripped; the judge sees only "Solution A" / "B".
#   * POSITION BIAS: every pair is judged in BOTH orderings (on-first, off-first)
#     and the votes are pooled, so a judge that just favours position A cancels out
#     (and shows up as low "position-consistency").
#   * SEPARATE + MARKED: reported in its own section flagged NON-DETERMINISTIC,
#     never mixed with the hard scorers.


@dataclass
class JudgeComparison:
    task_id: str
    pair_id: int
    ordering: str               # "a_first" | "b_first"
    winner_arm: str | None      # the winning arm LABEL, "tie", or None (unparseable)
    reason: str = ""
    pair: str = ""              # comparison key, e.g. "skill-a_vs_skill-b"
    a_label: str = ""          # the two arm labels compared in this pair
    b_label: str = ""


def _extract_json(text: str) -> dict | None:
    """Pull a JSON object out of a model reply (tolerates a code fence / prose)."""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        nl = t.find("\n")
        if nl != -1 and t[:nl].strip().lower() in ("json", ""):
            t = t[nl + 1:]
    try:
        v = json.loads(t)
        return v if isinstance(v, dict) else None
    except json.JSONDecodeError:
        pass
    a, b = t.find("{"), t.rfind("}")
    if 0 <= a < b:
        try:
            v = json.loads(t[a:b + 1])
            return v if isinstance(v, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def _redact_names(text: str, names: set[str]) -> str:
    """Scrub skill names out of a diff before the blind judge sees it, so an arm's
    own code mentioning its skill name can't leak which arm produced it."""
    for name in names:
        if name:
            text = re.sub(re.escape(name), "the-skill", text, flags=re.IGNORECASE)
    return text


def _judge_prompt(task_prompt: str, resp_a: str, resp_b: str, axis: str) -> str:
    return (
        "You are a meticulous, impartial code reviewer comparing two solutions to "
        "the SAME task, each shown as a unified git diff. Judge ONLY on: " + axis +
        ". Use only what is shown; if the two are genuinely indistinguishable on "
        "that axis, answer \"tie\".\n\n"
        "TASK:\n" + task_prompt + "\n\n"
        "=== SOLUTION A (git diff) ===\n" + (resp_a or "(empty diff)") + "\n\n"
        "=== SOLUTION B (git diff) ===\n" + (resp_b or "(empty diff)") + "\n\n"
        "Respond with ONLY a compact JSON object, no prose, no code fence:\n"
        "{\"winner\": \"A\" | \"B\" | \"tie\", \"reason\": \"<=20 words\"}"
    )


def _map_winner(ordering: str, ab: str | None, a_label: str, b_label: str) -> str | None:
    """Translate the judge's blind A/B verdict back to an arm LABEL, using the
    ordering WE (not the judge) chose. This is where blinding is undone, safely.
    In 'a_first' the judge's slot A held a_label's diff; in 'b_first' it held b_label's."""
    v = (ab or "").strip().lower()
    if v == "tie":
        return "tie"
    if v not in ("a", "b"):
        return None
    if ordering == "a_first":
        return a_label if v == "a" else b_label
    return b_label if v == "a" else a_label


def _judge_pairs(on_diffs: list[str], off_diffs: list[str], max_pairs: int,
                 rng: random.Random) -> list[tuple[str, str]]:
    if not on_diffs or not off_diffs:
        return []
    on, off = list(on_diffs), list(off_diffs)
    rng.shuffle(on)
    rng.shuffle(off)
    n = min(len(on), len(off), max_pairs)
    return [(on[i], off[i]) for i in range(n)]


def _judge_argv(prompt: str, cfg: ExperimentConfig) -> list[str]:
    """Argv for one blind `claude -p` judgement. The judge only emits a JSON
    verdict over attacker-influenced diff text, so it gets an EMPTY allow-list:
    a prompt injection hidden in a diff cannot reach the filesystem or shell
    because no tool is granted. Slash-commands + ambient MCP stay off so a
    globally-installed skill can't bias the verdict."""
    return [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--model", cfg.judge_model,
        "--max-turns", "1",
        "--permission-mode", cfg.permission_mode,
        "--allowedTools", "",          # empty allow-list -> judge has NO tools
        "--disable-slash-commands",    # no skill can fire in the judge
        "--strict-mcp-config",         # ignore ambient MCP servers
    ]


async def _judge_call(prompt: str, cfg: ExperimentConfig,
                      sem: asyncio.Semaphore) -> dict | None:
    """One blind judgement via `claude -p` in a throwaway dir. Runs with
    `--disable-slash-commands` so the skill UNDER TEST (which may be installed
    globally in ~/.claude) cannot bias the judge, and `--strict-mcp-config` to
    drop ambient MCP servers. (Auth and the host's global CLAUDE.md still apply --
    keep the judge host neutral.) Returns the parsed {winner, reason} or None."""
    async with sem:
        tmp = tempfile.mkdtemp(prefix="skill_ab_judge_")
        try:
            cmd = _judge_argv(prompt, cfg)
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=tmp,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            try:
                out, _ = await asyncio.wait_for(proc.communicate(), cfg.agent_timeout_s)
            except asyncio.TimeoutError:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError, OSError):
                    proc.kill()
                try:                          # bounded drain; never hang the slot
                    await asyncio.wait_for(proc.communicate(), 10.0)
                except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                    pass
                return None
            try:
                payload = json.loads(out.decode(errors="replace") or "{}")
            except json.JSONDecodeError:
                return None
            return _extract_json(payload.get("result") or "")
        finally:
            await asyncio.to_thread(shutil.rmtree, tmp, True)


async def run_qualitative_judge(results: list[RunResult], tasks: list[Task],
                                cfg: ExperimentConfig, seed: int = 0
                                ) -> list[JudgeComparison]:
    """Blind head-to-head over EVERY comparison pair (2-arm: skill vs control;
    3-arm: each skill vs control + A vs B). Each pair's ITT-valid diffs are judged
    in both orderings, concurrently, to cancel position bias."""
    rng = random.Random(seed)
    sem = asyncio.Semaphore(cfg.max_concurrency)
    by_arm_task: dict[tuple[Arm, str], list[str]] = {}
    for r in results:
        if r.itt_valid and r.diff:
            by_arm_task.setdefault((r.arm, r.task_id), []).append(r.diff)

    prompts_by_id = {t.id: t.prompt for t in tasks} if tasks else {}
    task_ids = ([t.id for t in tasks] if tasks
                else sorted({tid for _arm, tid in by_arm_task}))
    m = cfg.judge_max_diff_chars
    jobs, meta = [], []
    for arm_a, arm_b in experiment_pairs(cfg):
        la, lb = arm_label(cfg, arm_a), arm_label(cfg, arm_b)
        pkey = pair_key(cfg, arm_a, arm_b)
        for tid in task_ids:
            pairs = _judge_pairs(by_arm_task.get((arm_a, tid), []),
                                 by_arm_task.get((arm_b, tid), []),
                                 cfg.judge_max_pairs_per_task, rng)
            prompt = prompts_by_id.get(tid, tid)
            names = all_skill_names(cfg)
            for pid, (da, db) in enumerate(pairs):
                for ordering, x, y in (("a_first", da, db), ("b_first", db, da)):
                    meta.append((pkey, la, lb, tid, pid, ordering))
                    xr = _redact_names(x[:m], names)
                    yr = _redact_names(y[:m], names)
                    jobs.append(_judge_call(_judge_prompt(prompt, xr, yr,
                                                          cfg.judge_axis), cfg, sem))

    raw = await asyncio.gather(*jobs, return_exceptions=True)
    comparisons: list[JudgeComparison] = []
    for (pkey, la, lb, tid, pid, ordering), res in zip(meta, raw):
        winner = (None if isinstance(res, Exception) or not res
                  else _map_winner(ordering, res.get("winner"), la, lb))
        reason = ("judge failed" if isinstance(res, Exception) or not res
                  else str(res.get("reason", ""))[:120])
        comparisons.append(JudgeComparison(tid, pid, ordering, winner, reason, pkey, la, lb))
    return comparisons


def judge_winrate_ci(comps: list[JudgeComparison], iters: int, alpha: float,
                     rng: random.Random) -> tuple[float, float] | None:
    """Cluster-bootstrap CI for one pair's win_rate_a. Mirrors cluster_bootstrap_ci:
    resample TASKS with replacement, then judged comparisons within each sampled
    task, recomputing win_rate_a = a_wins / decisive per iteration. Resamples whose
    decisive count is 0 (all ties/failed) are skipped; returns None if no resample
    is ever decisive (e.g. every comparison was a tie) so callers can show no CI
    instead of dividing by zero."""
    if not comps:
        return None
    la, lb = comps[0].a_label, comps[0].b_label
    by_task: dict[str, list[JudgeComparison]] = {}
    for c in comps:
        by_task.setdefault(c.task_id, []).append(c)
    tasks = list(by_task)
    rates: list[float] = []
    for _ in range(iters):
        picks = [rng.choice(tasks) for _ in tasks]
        a_wins = b_wins = 0
        for t in picks:
            rows = by_task[t]
            for _ in rows:
                w = rng.choice(rows).winner_arm
                if w == la:
                    a_wins += 1
                elif w == lb:
                    b_wins += 1
        dec = a_wins + b_wins
        if dec:
            rates.append(a_wins / dec)
    if not rates:
        return None
    rates.sort()
    lo = rates[int((alpha / 2) * len(rates))]
    hi = rates[min(len(rates) - 1, int((1 - alpha / 2) * len(rates)))]
    return (lo, hi)


def aggregate_judge(comparisons: list[JudgeComparison], *, ci_iters: int = 0,
                    alpha: float = 0.05, rng: random.Random | None = None) -> dict:
    """Per-comparison-pair aggregation. Returns {pair_key: {a_label, b_label,
    a_wins, b_wins, ties, failed, decisive, win_rate_a, win_rate_a_ci, consistent,
    total_pairs}}. win_rate_a = a_label wins / decisive (0.5 = no preference). The
    cluster-bootstrap win_rate_a_ci is computed only when ci_iters>0 AND rng is
    given (else None), so existing callers are unaffected."""
    by_pair: dict[str, list[JudgeComparison]] = {}
    for c in comparisons:
        by_pair.setdefault(c.pair, []).append(c)
    out = {}
    for pkey, comps in by_pair.items():
        la, lb = comps[0].a_label, comps[0].b_label
        a_wins = sum(1 for c in comps if c.winner_arm == la)
        b_wins = sum(1 for c in comps if c.winner_arm == lb)
        ties = sum(1 for c in comps if c.winner_arm == "tie")
        failed = sum(1 for c in comps if c.winner_arm is None)
        decisive = a_wins + b_wins
        groups: dict[tuple[str, int], dict[str, str | None]] = {}
        for c in comps:
            groups.setdefault((c.task_id, c.pair_id), {})[c.ordering] = c.winner_arm
        consistent = total = 0
        for d in groups.values():
            af, bf = d.get("a_first"), d.get("b_first")
            if af is not None and bf is not None:   # both orderings produced a verdict
                total += 1
                if af == bf:
                    consistent += 1
        ci = (judge_winrate_ci(comps, ci_iters, alpha, rng)
              if ci_iters and rng is not None else None)
        out[pkey] = {"a_label": la, "b_label": lb, "a_wins": a_wins, "b_wins": b_wins,
                     "ties": ties, "failed": failed, "decisive": decisive,
                     "win_rate_a": (a_wins / decisive if decisive else float("nan")),
                     "win_rate_a_ci": ci,
                     "consistent": consistent, "total_pairs": total}
    return out


def build_judge_report(comparisons: list[JudgeComparison], cfg: ExperimentConfig,
                       results: list[RunResult] | None = None, seed: int = 0) -> str:
    banner = "## Qualitative — blind LLM judge (NON-DETERMINISTIC)"
    coverage = None
    if results is not None:
        cov = []
        for arm in experiment_arms(cfg):
            valid = [r for r in results if r.arm is arm and r.itt_valid]
            cov.append(f"{arm_label(cfg, arm)} {sum(1 for r in valid if r.diff)}/{len(valid)}")
        coverage = ("- diff coverage (ITT-valid runs with a usable diff): "
                    + " | ".join(cov) + " (empty/failed diffs are not judged)")
    if not comparisons:
        msg = (f"{banner}\n_No judged pairs: need ITT-valid runs WITH captured diffs "
               f"in at least two arms (set capture_diffs=True and judge_enabled=True)._")
        return msg + ("\n" + coverage if coverage else "")

    lines = [
        banner,
        f"_Judge {cfg.judge_model}; axis: {cfg.judge_axis}. Arm labels stripped; every "
        f"pair judged in BOTH orderings to cancel position bias. NON-DETERMINISTIC — "
        f"reported separately from the hard scorers; do not treat as a settled fact._",
    ]
    if coverage:
        lines.append(coverage)
    for pkey, agg in aggregate_judge(
            comparisons, ci_iters=cfg.bootstrap_iters,
            alpha=cfg.bootstrap_alpha, rng=random.Random(seed)).items():
        la, lb = agg["a_label"], agg["b_label"]
        wr = agg["win_rate_a"]
        wr_s = f"{wr:.3f}" if wr == wr else "n/a"
        ci = agg["win_rate_a_ci"]
        ci_s = f" [{ci[0]:.3f}, {ci[1]:.3f}]" if ci else ""
        cons = (f"{100 * agg['consistent'] / agg['total_pairs']:.0f}%"
                if agg["total_pairs"] else "n/a")
        verdict = "no decisive preference"
        if agg["decisive"]:
            if wr > 0.5:
                verdict = f"judge prefers **{la}**"
            elif wr < 0.5:
                verdict = f"judge prefers **{lb}**"
        lines += [
            "",
            f"### {la} vs {lb}",
            f"- **{la} win rate: {wr_s}{ci_s}** over {lb}  ({agg['a_wins']}–{agg['b_wins']}, "
            f"{agg['ties']} tie, {agg['failed']} unparseable; 0.5 = no preference)",
            f"- position-consistency: {cons} of pairs agreed across both orderings "
            f"(low = order-sensitive / noisy — discount)",
            f"- verdict: {verdict}",
        ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reproducibility manifest + portable summary
# ---------------------------------------------------------------------------

def experiment_manifest(cfg: ExperimentConfig, seed: int = 0,
                        timestamp: float | None = None, offline: bool = False) -> dict:
    """Provenance for a run. Best-effort: external calls that fail record None,
    never raise. `timestamp` is injected so reports are reproducible in tests.
    `offline=True` skips ALL subprocess calls (claude/git/file hash) -- used by the
    demo so it touches nothing."""
    def _try(fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return None
    if offline:
        cli_version = base_sha = skill_hash = skill_b_hash = None
    else:
        cli_version = _try(lambda: subprocess.run(
            ["claude", "--version"], capture_output=True, text=True, timeout=30).stdout.strip())
        base_sha = _try(lambda: _git(cfg.repo_path, ["rev-parse", cfg.base_ref]).stdout.strip())
        skill_md = cfg.skill_src / "SKILL.md"
        skill_hash = _try(lambda: hashlib.sha256(skill_md.read_bytes()).hexdigest())
        skill_b_hash = (_try(lambda: hashlib.sha256(
            (cfg.skill_b_src / "SKILL.md").read_bytes()).hexdigest())
            if cfg.skill_b_src else None)
    return {
        "harness_version": __version__,
        "claude_cli_version": cli_version,
        "model": cfg.model,
        # Per-arm models (reproducibility for model comparisons). None for command
        # arms (no claude model). Per-arm runners record the agent CLI of each arm.
        "arm_models": {arm_label(cfg, a): arm_model(cfg, a)
                       for a in experiment_arms(cfg)},
        "arm_runners": {arm_label(cfg, a): _runner_label(arm_runner(cfg, a))
                        for a in experiment_arms(cfg)},
        "repo_path": str(cfg.repo_path),
        "base_ref": cfg.base_ref,
        "base_ref_sha": base_sha,
        "skill_name": cfg.skill_name,
        "skill_md_sha256": skill_hash,
        "skill_b_name": cfg.skill_b_name,
        "skill_b_md_sha256": skill_b_hash,
        "k": cfg.k,
        "seed": seed,
        "bootstrap_iters": cfg.bootstrap_iters,
        "permutation_iters": cfg.permutation_iters,
        "alpha": cfg.bootstrap_alpha,
        "platform": platform.platform(),
        "timestamp": timestamp,
    }


def summary_dict(results: list[RunResult], cfg: ExperimentConfig, manifest: dict,
                 scorers: list[Scorer] | None = None, seed: int = 0) -> dict:
    """Portable, schema-versioned summary (v2): per-arm validity + every pairwise
    comparison (2-arm: skill vs control; 3-arm: each skill vs control + A vs B). A
    convenience `itt`/`validity` mirror of the PRIMARY pair keeps simple consumers
    (badge, CI) working. Reuses the SAME estimators build_report uses."""
    scorers = scorers or default_scorers(cfg)
    metrics = [s.name for s in scorers] + ["cost_usd"]

    est = compute_estimates(results, cfg, metrics, seed)   # one shared pass
    comparisons = {
        key: {"a": p["a"], "b": p["b"],
              "itt": {m: _estimate_fields(e) for m, e in p["itt"].items() if e},
              "per_protocol": {m: _estimate_fields(e) for m, e in p["pp"].items() if e}}
        for key, p in est.items()}

    arm_stats = {}
    for arm in experiment_arms(cfg):
        fired, clean = activation_rate(results, arm)
        arm_stats[arm_label(cfg, arm)] = {
            "valid": sum(1 for r in results if r.arm is arm and r.itt_valid),
            "activation_fired": fired, "activation_clean": clean,
            "contaminated": sum(1 for r in results if r.arm is arm and r.contaminated)}

    pa, pb = primary_pair(cfg)
    pkey = pair_key(cfg, pa, pb)
    return {
        "schema_version": 2,
        "manifest": manifest,
        "primary_metric": cfg.primary_metric,
        "primary_pair": pkey,
        "arms": arm_stats,
        "comparisons": comparisons,
        # convenience mirror of the PRIMARY comparison (badge / simple consumers):
        "itt": comparisons[pkey]["itt"],
        "per_protocol": comparisons[pkey]["per_protocol"],
        "validity": {
            "on_valid": arm_stats[arm_label(cfg, pa)]["valid"],
            "off_valid": arm_stats[arm_label(cfg, pb)]["valid"],
            "off_contaminated": sum(1 for r in results if r.contaminated),
        },
    }


def write_summary(results: list[RunResult], cfg: ExperimentConfig, manifest: dict,
                  seed: int = 0) -> Path:
    cfg.results_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.results_dir / "summary.json"
    path.write_text(json.dumps(summary_dict(results, cfg, manifest, seed=seed), indent=2))
    return path


def load_results(path: Path) -> list[RunResult]:
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(RunResult.from_dict(json.loads(line)))
        except (json.JSONDecodeError, KeyError):
            continue   # tolerate a partial trailing line from a killed run (resume)
    return out


def _check_resume_compatible(manifest_path: Path, manifest: dict) -> None:
    """Refuse to resume into an incomparable experiment. The control variables
    (SKILL.md, base commit, model, k) must match the persisted manifest; a missing
    SKILL.md hash means we cannot verify the invariant and also refuse."""
    if not manifest_path.exists():
        return
    old = json.loads(manifest_path.read_text())
    if not (old.get("skill_md_sha256") and manifest.get("skill_md_sha256")):
        raise SystemExit("resume aborted: cannot verify the SKILL.md hash of the "
                         "persisted run; start fresh in a clean results_dir.")
    for key, label in (("skill_md_sha256", "SKILL.md"), ("skill_b_md_sha256", "skill B SKILL.md"),
                       ("base_ref_sha", "base commit"), ("model", "model"), ("k", "k")):
        if old.get(key) != manifest.get(key):
            raise SystemExit(f"resume aborted: {label} changed since the persisted run "
                             f"({old.get(key)} -> {manifest.get(key)}). Use a fresh "
                             f"results_dir.")


def _metric_direction(cfg: ExperimentConfig, metric: str) -> int:
    for s in default_scorers(cfg):
        if s.name == metric:
            return s.direction
    return -1 if metric in ("cost_usd", "diff_lines") else 1


# ---------------------------------------------------------------------------
# Verdict badge
# ---------------------------------------------------------------------------

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
    else:
        improved = (point > 0) if direction > 0 else (point < 0)
        label = "verified" if improved else "regressed"
        color = "brightgreen" if improved else "red"
    return {"label": label, "color": color, "point": point,
            "ci_low": lo, "ci_high": hi, "n_tasks": n_tasks}


def render_badge_svg(metric: str, verdict: dict) -> str:
    """A flat shields-style SVG. No external assets/deps."""
    pct = f"{verdict['point'] * 100:+.0f}%"
    right = (f"{verdict['label']} {metric} {pct}" if verdict["label"] != "inconclusive"
             else f"{metric}: inconclusive")
    right = html.escape(right)
    color = {"brightgreen": "#4c1", "red": "#e05d44", "lightgrey": "#9f9f9f"}[verdict["color"]]
    lw, rw = 70, max(120, 8 * len(right))
    w = lw + rw
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="20" '
        f'role="img" aria-label="skill A/B: {right}">'
        f'<rect width="{lw}" height="20" fill="#555"/>'
        f'<rect x="{lw}" width="{rw}" height="20" fill="{color}"/>'
        f'<g fill="#fff" font-family="Verdana,Geneva,sans-serif" font-size="11">'
        f'<text x="6" y="14">skill A/B</text>'
        f'<text x="{lw + 6}" y="14">{right}</text></g></svg>')


def badge_endpoint_json(metric: str, verdict: dict) -> dict:
    """shields.io 'endpoint' schema (host the JSON, point shields at it)."""
    pct = f"{verdict['point'] * 100:+.0f}%"
    msg = f"{verdict['label']} {pct}" if verdict["label"] != "inconclusive" else "inconclusive"
    return {"schemaVersion": 1, "label": f"skill A/B: {metric}",
            "message": msg, "color": verdict["color"]}


def badge_markdown(metric: str, verdict: dict, svg_rel_path: str) -> str:
    ci = f"[{verdict['ci_low'] * 100:+.0f}%, {verdict['ci_high'] * 100:+.0f}%]"
    return (f"![skill A/B {metric}]({svg_rel_path}) "
            f"<!-- {verdict['label']} {metric} {verdict['point'] * 100:+.0f}% 95% CI {ci}, "
            f"n_tasks={verdict['n_tasks']} -->")


def primary_verdict(summary: dict, cfg: ExperimentConfig) -> dict | None:
    """The badge_verdict for the configured primary metric, or None."""
    metric = summary["primary_metric"]
    est = summary["itt"].get(metric)
    if not est:
        return None
    return badge_verdict(est, _metric_direction(cfg, metric), est["n_tasks"],
                         est["clustered"], summary["validity"]["off_contaminated"])


def _gallery_verdict_from_summary(summary: dict) -> dict | None:
    """Badge verdict for a foreign summary.json WITHOUT a cfg. The gallery ingests
    many summaries and has no ExperimentConfig, so metric direction falls back to
    the same default _metric_direction uses for unknown metrics; a custom lower-is-
    better scorer can't be detected from a summary alone (open question)."""
    metric = summary.get("primary_metric")
    est = (summary.get("itt") or {}).get(metric)
    if not est:
        return None
    direction = -1 if metric in ("cost_usd", "diff_lines") else 1
    return badge_verdict(est, direction, est["n_tasks"], est["clustered"],
                         summary.get("validity", {}).get("off_contaminated", 0))


def _gallery_unsupported_card(summary: dict) -> str:
    v = html.escape(str(summary.get("schema_version")))
    return ("<div class='arm'><div class='arm-head'><span class='arm-name'>"
            f"unsupported schema v{v}</span></div>"
            "<p class='note'>cannot render — regenerate with a current harness.</p></div>")


def _gallery_card(summary: dict, href: str | None) -> str:
    verdict = _gallery_verdict_from_summary(summary)
    metric = summary.get("primary_metric", "?")
    badge = render_badge_svg(metric, verdict) if verdict else ""
    skill = html.escape(str(summary.get("manifest", {}).get("skill_name", "?")))
    pair = html.escape(str(summary.get("primary_pair", "")))
    link = (f"<a href='{html.escape(href)}'>open report →</a>" if href
            else "<span class='note'>no report linked</span>")
    return (f"<div class='arm'><div class='arm-head'><span class='arm-name'>{skill}"
            f"</span></div><div class='badge'>{badge}</div>"
            f"<p class='note'>{pair} · {link}</p></div>")


def build_gallery_html(entries: list[dict]) -> str:
    """Static index over many runs. entry = {"summary": <summary.json dict>,
    "report_href": <str|None>}. UNVERIFIED by construction: summary.json is self-
    reported, so the page says so. Reuses _HTML_STYLE + render_badge_svg and links
    OUT to each per-run report.html instead of embedding it."""
    cards = []
    for e in entries:
        s = e.get("summary") or {}
        if s.get("schema_version") != 2:
            cards.append(_gallery_unsupported_card(s))   # never crash on old/forged
            continue
        cards.append(_gallery_card(s, e.get("report_href")))
    disclaimer = ("<p class='note'>Every entry below is <b>self-reported</b> and "
                  "<b>unverified</b> — reproduce via <code>skill-ab run "
                  "--from-github</code>.</p>")
    return "".join([
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        "<title>skill A/B — gallery</title>",
        f"<style>{_HTML_STYLE}</style></head><body><div class='wrap'>",
        "<h1>skill A/B gallery</h1>", disclaimer,
        "".join(cards), "</div></body></html>",
    ])


def discover_runs(root: Path) -> list[dict]:
    """Scan `root` for run subdirs containing summary.json; return RunCard dicts
    newest-first (by manifest timestamp). Tolerates malformed/partial run dirs
    (skips them, never raises) so a half-written run can't break the history view."""
    out: list[dict] = []
    if not root.exists():
        return out
    for d in sorted(root.iterdir()):
        if not d.is_dir() or not (d / "summary.json").exists():
            continue
        try:
            s = json.loads((d / "summary.json").read_text())
        except (json.JSONDecodeError, OSError):
            continue
        man = s.get("manifest") or {}
        verdict = _gallery_verdict_from_summary(s)
        arms = s.get("arms") or {}
        valid = sum(a.get("valid", 0) for a in arms.values() if isinstance(a, dict))
        cost = None
        rp = d / "results.jsonl"
        if rp.exists():
            try:
                cost = sum((json.loads(ln).get("cost_usd") or 0)
                           for ln in rp.read_text().splitlines() if ln.strip())
            except (json.JSONDecodeError, OSError):
                cost = None
        out.append({
            "id": d.name, "skill_a": man.get("skill_name"),
            "skill_b": man.get("skill_b_name"), "target": man.get("base_ref"),
            "primary_metric": s.get("primary_metric"),
            "verdict": verdict["label"] if verdict else None,
            "created_ts": man.get("timestamp"), "n_valid": valid, "cost_usd": cost,
            "status": "done",
            "report_url": (f"/api/runs/{d.name}/report"
                           if (d / "report.html").exists() else None),
            "badge_url": (f"/api/runs/{d.name}/badge"
                          if (d / "badge.svg").exists() else None)})
    out.sort(key=lambda r: (r.get("created_ts") or 0), reverse=True)
    return out


# ---------------------------------------------------------------------------
# Self-contained HTML report
# ---------------------------------------------------------------------------

# ---- parsed unified-diff model + renderer (plan 024 §1.1-§1.4) -------------
# A pure parser (`re`/`html`/`difflib`, no regex on content) builds a
# _PatchFile/_Hunk/_Row model; the renderer emits escaped, line-numbered,
# word-highlighted, context-folded DOM. Diff bytes are escaped PER SEGMENT at
# render time and never reach the JSON blob or any innerHTML-of-raw path.

@dataclass
class _Row:
    """One diff line. `segments` is [(text, changed)] carrying RAW (un-escaped)
    text -- escaping happens at render so the model stays pure data. `kind`
    'fold' rows are synthetic collapse markers carrying the hidden run."""
    kind: str                                  # 'ctx'|'add'|'del'|'meta'|'fold'
    old_n: int | None = None
    new_n: int | None = None
    segments: list[tuple[str, bool]] = field(default_factory=list)
    pair_idx: int | None = None
    side: str | None = None                    # 'o'|'n'|'b'
    fold_n: int | None = None
    fold_rows: list["_Row"] = field(default_factory=list)


@dataclass
class _Hunk:
    header_text: str
    old_start: int
    new_start: int
    rows: list[_Row] = field(default_factory=list)
    hunk_id: str = ""


@dataclass
class _PatchFile:
    path: str
    old_path: str
    status: str                                # 'A'|'M'|'D'|'R'
    add_count: int = 0
    del_count: int = 0
    hunks: list[_Hunk] = field(default_factory=list)
    file_id: str = ""


_DIFF_GIT_RE = re.compile(r"^diff --git a/(\S+) b/(\S+)")
_HUNK_RE = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
_STATUS_TIP = {"A": "added", "M": "modified", "D": "deleted", "R": "renamed"}
_ROW_K = {"ctx": 0, "add": 1, "del": 2}
_ROW_SIGN = {"ctx": " ", "add": "+", "del": "-"}


def parse_unified_diff(diff: str | None) -> list["_PatchFile"]:
    """Parse `git diff` text into a [_PatchFile] model. Regex only ever touches diff
    *control* lines (`diff --git`, `@@`); content bytes are carried verbatim. The
    `+++`/`---`/`index`/`new file`/`deleted file`/`rename` lines are FILE METADATA
    (status + paths), never `ctx` rows -- fixing the old `_diff_to_html` fall-through
    that tinted them as context. Old/new line numbers are seeded from each hunk
    header and advanced per row (old# on ctx/del, new# on ctx/add); +adds/-dels are
    counted per file."""
    files: list[_PatchFile] = []
    cur: _PatchFile | None = None
    hunk: _Hunk | None = None
    old_n = new_n = 0
    for line in (diff or "").splitlines():
        gm = _DIFF_GIT_RE.match(line)
        if gm:
            cur = _PatchFile(path=gm.group(2), old_path=gm.group(1), status="M")
            files.append(cur)
            hunk = None
            continue
        if cur is None:
            continue                            # preamble before the first file
        hm = _HUNK_RE.match(line)
        if hm:
            old_n, new_n = int(hm.group(1)), int(hm.group(2))
            hunk = _Hunk(header_text=line, old_start=old_n, new_start=new_n)
            cur.hunks.append(hunk)
            continue
        if line.startswith("\\"):               # "\ No newline at end of file"
            continue
        if hunk is None:
            # File metadata precedes the first @@: classify status, drop the rest.
            if line.startswith("new file"):
                cur.status = "A"
            elif line.startswith("deleted file"):
                cur.status = "D"
            elif line.startswith("rename from"):
                cur.status, cur.old_path = "R", line[12:].strip() or cur.old_path
            elif line.startswith("rename to"):
                cur.status, cur.path = "R", line[10:].strip() or cur.path
            elif line[:1] in "+- " and not line.startswith(("+++ ", "--- ")):
                # A diff body with no @@ header (synthetic fixtures): seed an
                # implicit hunk at 1/1 so the content still renders.
                hunk = _Hunk(header_text="", old_start=1, new_start=1)
                old_n = new_n = 1
                cur.hunks.append(hunk)
            else:
                continue                        # index/mode/---/+++/binary/blank
            if hunk is None:
                continue
        c = line[:1]
        if c == "+":
            hunk.rows.append(_Row("add", None, new_n, [(line[1:], False)]))
            new_n += 1
            cur.add_count += 1
        elif c == "-":
            hunk.rows.append(_Row("del", old_n, None, [(line[1:], False)]))
            old_n += 1
            cur.del_count += 1
        else:
            txt = line[1:] if c == " " else line
            hunk.rows.append(_Row("ctx", old_n, new_n, [(txt, False)]))
            old_n += 1
            new_n += 1
    for fi, pf in enumerate(files):
        pf.file_id = f"f-{fi}"
        for hi, hk in enumerate(pf.hunks):
            hk.hunk_id = f"{fi}-{hi}"
    return files


def word_diff_pairs(hunk: "_Hunk", ratio_gate: float = 0.3,
                    max_len: int = 400) -> "_Hunk":
    """Pair the i-th del row with the i-th add row in the hunk and compute intra-line
    char-level highlight segments, mutating each row's `segments`/`pair_idx`/`side`.
    A pair runs the char diff only when SequenceMatcher.ratio() >= ratio_gate AND the
    longer line is <= max_len (the cap dodges O(n*m) stalls on minified lines);
    otherwise the line keeps its single whole-line segment (changed=False)."""
    dels = [r for r in hunk.rows if r.kind == "del"]
    adds = [r for r in hunk.rows if r.kind == "add"]
    for idx, (dr, ar) in enumerate(zip(dels, adds)):
        d = dr.segments[0][0] if dr.segments else ""
        a = ar.segments[0][0] if ar.segments else ""
        dr.pair_idx = ar.pair_idx = idx
        dr.side, ar.side = "o", "n"
        if max(len(d), len(a)) > max_len:
            continue
        sm = difflib.SequenceMatcher(None, d, a, autojunk=False)
        if sm.ratio() < ratio_gate:
            continue
        dseg: list[tuple[str, bool]] = []
        aseg: list[tuple[str, bool]] = []
        for tag, i1, i2, j1, j2 in sm.get_opcodes():
            if tag == "equal":
                dseg.append((d[i1:i2], False))
                aseg.append((a[j1:j2], False))
            elif tag == "delete":
                dseg.append((d[i1:i2], True))
            elif tag == "insert":
                aseg.append((a[j1:j2], True))
            else:                               # replace
                dseg.append((d[i1:i2], True))
                aseg.append((a[j1:j2], True))
        dr.segments, ar.segments = dseg, aseg
    return hunk


def fold_context(rows: list["_Row"], ctx: int = 3, min_run: int = 6) -> list["_Row"]:
    """Collapse runs of >= min_run consecutive ctx rows into one 'fold' marker,
    keeping `ctx` lead + `ctx` trail rows visible and stashing the hidden rows on the
    marker so the client can reveal real captured context (never a faked fetch). Runs
    with nothing left to hide after the lead/trail are returned untouched."""
    out: list[_Row] = []
    i, n = 0, len(rows)
    while i < n:
        if rows[i].kind != "ctx":
            out.append(rows[i])
            i += 1
            continue
        j = i
        while j < n and rows[j].kind == "ctx":
            j += 1
        run = rows[i:j]
        hidden = run[ctx:len(run) - ctx]
        if len(run) >= min_run and hidden:
            out.extend(run[:ctx])
            out.append(_Row("fold", fold_n=len(hidden), fold_rows=hidden))
            out.extend(run[len(run) - ctx:])
        else:
            out.extend(run)
        i = j
    return out


def _render_seg_html(segments: list[tuple[str, bool]]) -> str:
    """Escape EACH segment individually; wrap changed segments in <span class=w>. No
    raw diff byte is ever interpolated unescaped (load-bearing, plan 024 §1.4)."""
    out = []
    for text, changed in segments:
        esc = html.escape(text)
        out.append(f'<span class="w">{esc}</span>' if changed else esc)
    return "".join(out)


def _render_row(row: "_Row") -> str:
    if row.kind == "fold":
        inner = "".join(_render_row(r) for r in row.fold_rows)
        return (f'<div class="fold" data-n="{row.fold_n}">'
                f'<button type="button" data-act="expand" aria-expanded="false">'
                f'Expand {row.fold_n} unchanged lines</button>'
                f'<div class="folded" hidden>{inner}</div></div>')
    old = "" if row.old_n is None else str(row.old_n)
    new = "" if row.new_n is None else str(row.new_n)
    pair = f' data-pair="{row.pair_idx}"' if row.pair_idx is not None else ""
    return (f'<div class="row {row.kind}" data-k="{_ROW_K.get(row.kind, 0)}"{pair}>'
            f'<span class="ln">{old}</span><span class="ln">{new}</span>'
            f'<span class="sg">{_ROW_SIGN.get(row.kind, " ")}</span>'
            f'<span class="tx">{_render_seg_html(row.segments)}</span></div>')


def _render_patch(patch_files: list["_PatchFile"], patch_id: str,
                  truncated: bool = False) -> str:
    """Escaped DOM for one run's diff: a sticky file head (path + `+N -M` + A/M/D/R
    status) per file, a head per hunk, a CSS-grid row [old# new# sign code] per line,
    and a visible truncation marker when the captured diff was cut."""
    trunc = ('<div class="trunc" role="note">diff truncated &mdash; only the first '
             'captured characters are shown</div>') if truncated else ""
    if not patch_files:
        return f'<div class="patch"><p class="empty">(no diff)</p>{trunc}</div>'
    pid = html.escape(patch_id)
    out = ['<div class="patch">']
    for fi, pf in enumerate(patch_files):
        out.append(
            f'<section class="file" id="f-{pid}-{fi}" '
            f'data-path="{html.escape(pf.path)}" data-add="{pf.add_count}" '
            f'data-del="{pf.del_count}" data-status="{pf.status}">')
        out.append(
            f'<div class="file-head"><span class="fpath">{html.escape(pf.path)}</span>'
            f'<span class="fstat s-{pf.status}" '
            f'data-tip="{_STATUS_TIP.get(pf.status, "modified")}">{pf.status}</span>'
            f'<span class="fcount">+{pf.add_count} {_MINUS}{pf.del_count}</span></div>')
        for hi, hk in enumerate(pf.hunks):
            word_diff_pairs(hk)
            rows = fold_context(hk.rows)
            out.append(f'<div class="hunk" id="h-{pid}-{fi}-{hi}">')
            if hk.header_text:
                out.append(f'<div class="hunk-head">{html.escape(hk.header_text)}</div>')
            out.extend(_render_row(r) for r in rows)
            out.append('</div>')
        out.append('</section>')
    out.append(trunc)
    out.append('</div>')
    return "".join(out)


def render_diff(diff: str | None, patch_id: str, truncated: bool = False) -> str:
    """Parse + render a unified diff into escaped, line-numbered, word-highlighted,
    context-folded DOM. Replaces the flat `_diff_to_html`."""
    return _render_patch(parse_unified_diff(diff), patch_id, truncated)


_MIDDOT = "·"
_MINUS = "−"
# Caret glyph for server-rendered <details> summaries; matches the JS IC.caret icon.
_CARET = ("<svg class='caret' width='13' height='13' viewBox='0 0 24 24' fill='none'>"
          "<path d='M9 6l6 6-6 6' stroke='currentColor' stroke-width='2' "
          "stroke-linecap='round' stroke-linejoin='round'/></svg>")


_HTML_STYLE = """
  :root{
    --bg:#f5f6f8; --bg-grad-a:#f8f9fb; --bg-grad-b:#eef1f5;
    --card:#ffffff; --card-2:#fbfcfd;
    --ink:#11151b; --ink-2:#39414c; --muted:#6b7484; --faint:#9aa3b1;
    --line:#e7e9ee; --line-2:#dde0e7;
    --good:#15803d; --good-bg:#e9f6ee; --good-line:#bfe6cd;
    --bad:#c01f1f; --bad-bg:#fcecec; --bad-line:#f1c9c9;
    --neutral:#6b7484;
    --grid:#eef0f4; --axis:#c7ccd6;
    --pill-grey-bg:#eef0f3; --pill-grey-ink:#5b6472;
    --hunk:#8957e5; --accent:#1f77b4; --diverge:#9a6b00;
    --w-add:color-mix(in srgb,var(--good) 32%, transparent);
    --w-del:color-mix(in srgb,var(--bad) 32%, transparent);
    --mark-bg:#ffe08a; --mark-ink:#3a2c00; --accent-ring:color-mix(in srgb,var(--accent) 40%, transparent);
    --rail-cur:color-mix(in srgb,var(--accent) 13%, var(--card));
    --shadow-sm:0 1px 2px rgba(17,21,27,.05);
    --shadow:0 1px 2px rgba(17,21,27,.05), 0 10px 30px rgba(17,21,27,.07);
    --shadow-lg:0 2px 4px rgba(17,21,27,.05), 0 24px 60px rgba(17,21,27,.10);
    --radius:16px; --radius-sm:11px;
    --mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace;
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,
      "Apple Color Emoji","Segoe UI Emoji",sans-serif;
  }
  @media (prefers-color-scheme:dark){
    :root{
      --bg:#0b0d11; --bg-grad-a:#0e1116; --bg-grad-b:#0a0c10;
      --card:#13171e; --card-2:#161b23;
      --ink:#eef1f6; --ink-2:#c5ccd6; --muted:#8b94a2; --faint:#69727f;
      --line:#232a34; --line-2:#2b333f;
      --good:#54d98a; --good-bg:#10271a; --good-line:#1f4a31;
      --bad:#f0807f; --bad-bg:#2a1416; --bad-line:#5a2a2c;
      --neutral:#8b94a2;
      --grid:#1b212b; --axis:#384353;
      --pill-grey-bg:#1d242e; --pill-grey-ink:#9aa3b1;
      --hunk:#b392f0; --accent:#5aa9dd; --diverge:#e3b341;
      --w-add:color-mix(in srgb,var(--good) 40%, transparent);
      --w-del:color-mix(in srgb,var(--bad) 40%, transparent);
      --mark-bg:#7a5b00; --mark-ink:#fff3cf; --accent-ring:color-mix(in srgb,var(--accent) 48%, transparent);
      --rail-cur:color-mix(in srgb,var(--accent) 22%, var(--card));
      --shadow-sm:0 1px 2px rgba(0,0,0,.4);
      --shadow:0 1px 2px rgba(0,0,0,.4), 0 14px 36px rgba(0,0,0,.5);
      --shadow-lg:0 2px 6px rgba(0,0,0,.45), 0 30px 70px rgba(0,0,0,.6);
    }
  }
  *{box-sizing:border-box}
  html,body{margin:0}
  body{
    font-family:var(--sans); color:var(--ink); background:var(--bg);
    background-image:
      radial-gradient(1200px 540px at 88% -8%,
        color-mix(in srgb,var(--bg-grad-b) 65%, transparent), transparent),
      linear-gradient(180deg, var(--bg-grad-a), var(--bg));
    background-attachment:fixed;
    -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
    font-size:14px; line-height:1.5;
  }
  .wrap{max-width:1080px; margin:0 auto; padding:32px 24px 72px}
  .num{font-variant-numeric:tabular-nums; font-feature-settings:"tnum" 1}
  .mono{font-family:var(--mono)}
  h1,h2,h3{margin:0; letter-spacing:-.01em}
  a{color:inherit}

  /* ---------- header ---------- */
  .topbar{display:flex; align-items:flex-start; justify-content:space-between;
    gap:20px; flex-wrap:wrap; margin-bottom:22px}
  .brand{display:flex; align-items:center; gap:11px}
  .brand .glyph{width:34px; height:34px; border-radius:9px; flex:0 0 auto;
    background:linear-gradient(135deg,#1f77b4,#5aa9dd); display:grid;
    place-items:center; box-shadow:var(--shadow-sm)}
  .brand .glyph svg{display:block}
  .eyebrow{font-size:11px; font-weight:650; letter-spacing:.10em;
    text-transform:uppercase; color:var(--muted)}
  .title{font-size:21px; font-weight:680; line-height:1.2; margin-top:1px}
  .title .vs{color:var(--faint); font-weight:500; padding:0 .28em}
  .subtitle{color:var(--muted); font-size:13px; margin-top:3px}
  .chips{display:flex; flex-wrap:wrap; gap:7px; align-items:center;
    justify-content:flex-end; max-width:560px}
  .chip{display:inline-flex; align-items:center; gap:6px; font-size:11.5px;
    color:var(--ink-2); background:var(--card); border:1px solid var(--line);
    border-radius:999px; padding:5px 10px; box-shadow:var(--shadow-sm);
    white-space:nowrap}
  .chip b{font-weight:640; color:var(--ink)}
  .chip .k{color:var(--muted); font-weight:550}
  .chip.dim{color:var(--muted)}
  .chip .dot{width:6px; height:6px; border-radius:50%}

  /* ---------- generic card ---------- */
  .card{background:var(--card); border:1px solid var(--line);
    border-radius:var(--radius); box-shadow:var(--shadow)}
  .section-h{display:flex; align-items:baseline; justify-content:space-between;
    gap:12px; margin:30px 2px 13px}
  .section-h h2{font-size:15.5px; font-weight:660}
  .section-h .hint{font-size:12px; color:var(--muted)}

  /* ---------- hero ---------- */
  .hero{position:relative; overflow:hidden; padding:26px 28px;
    display:grid; grid-template-columns:1.15fr .85fr; gap:30px;
    box-shadow:var(--shadow-lg)}
  .hero::before{content:""; position:absolute; inset:0; pointer-events:none;
    background:
      radial-gradient(620px 240px at 100% 0%,
        color-mix(in srgb,#1f77b4 9%, transparent), transparent 70%),
      radial-gradient(520px 220px at 0% 100%,
        color-mix(in srgb,#ff7f0e 6%, transparent), transparent 70%)}
  .hero > *{position:relative}
  .hero-l{min-width:0}
  .status-pill{display:inline-flex; align-items:center; gap:8px;
    font-size:12px; font-weight:640; letter-spacing:.02em;
    background:var(--pill-grey-bg); color:var(--pill-grey-ink);
    border:1px solid var(--line-2); border-radius:999px; padding:6px 13px}
  .status-pill .led{width:9px; height:9px; border-radius:50%;
    background:var(--faint);
    box-shadow:0 0 0 4px color-mix(in srgb,var(--faint) 22%, transparent)}
  .hero h1{font-size:clamp(24px,3.4vw,33px); font-weight:720; line-height:1.1;
    margin:16px 0 0; letter-spacing:-.02em}
  .hero h1 .accent{color:#1f77b4}
  .hero p.lede{color:var(--ink-2); font-size:13.5px; line-height:1.62;
    margin:13px 0 0; max-width:48ch}
  .hero p.lede b{color:var(--ink); font-weight:640}
  .hero-tags{display:flex; gap:8px; flex-wrap:wrap; margin-top:17px}
  .htag{font-size:11.5px; color:var(--muted); background:var(--card-2);
    border:1px solid var(--line); border-radius:8px; padding:4px 9px}

  .xrun{margin-top:15px; padding:12px 14px; border-radius:11px;
    border:1px solid color-mix(in srgb,var(--neutral) 40%,var(--line));
    background:color-mix(in srgb,var(--neutral) 9%,var(--card));
    font-size:12.5px; line-height:1.55; color:var(--ink-2)}
  .xrun b{color:var(--ink); font-weight:640}
  .xrun .xrchips{display:flex; gap:7px; flex-wrap:wrap; margin-top:9px}
  .xrun .xrchip{font-size:11.5px; font-family:var(--mono); background:var(--card-2);
    border:1px solid var(--line); border-radius:7px; padding:3px 8px; color:var(--ink)}
  .xrun .xrchip .arrow{color:var(--muted); margin:0 5px}

  .costcard{background:var(--card-2); border:1px solid var(--line);
    border-radius:13px; padding:18px 18px 16px; align-self:start;
    box-shadow:var(--shadow-sm)}
  .costcard .cap{font-size:11px; font-weight:640; letter-spacing:.08em;
    text-transform:uppercase; color:var(--muted)}
  .bignum{display:flex; align-items:baseline; gap:11px; margin-top:8px}
  .bignum .v{font-size:40px; font-weight:740; letter-spacing:-.025em;
    line-height:1}
  .delta-chip{display:inline-flex; align-items:center; gap:5px; font-size:12px;
    font-weight:660; padding:4px 9px; border-radius:999px}
  .delta-chip.good{color:var(--good); background:var(--good-bg);
    border:1px solid var(--good-line)}
  .delta-chip.bad{color:var(--bad); background:var(--bad-bg);
    border:1px solid var(--bad-line)}
  .bignum-sub{font-size:12px; color:var(--muted); margin-top:6px}
  .minibars{margin-top:16px; display:flex; flex-direction:column; gap:9px}
  .mbrow{display:grid; grid-template-columns:1fr; gap:4px}
  .mbtop{display:flex; align-items:center; justify-content:space-between;
    gap:8px; font-size:12px}
  .mbtop .nm{display:flex; align-items:center; gap:7px; min-width:0;
    color:var(--ink-2)}
  .mbtop .nm .sw{width:9px; height:9px; border-radius:3px; flex:0 0 auto}
  .mbtop .nm span{overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
  .mbtop .vv{font-weight:650; color:var(--ink)}
  .mbtrack{height:8px; border-radius:6px; background:var(--grid);
    overflow:hidden}
  .mbfill{height:100%; border-radius:6px}
  .mbtag{font-size:10px; font-weight:680; letter-spacing:.04em; padding:1px 6px;
    border-radius:5px; color:var(--good); background:var(--good-bg);
    border:1px solid var(--good-line)}

  /* ---------- arm cards ---------- */
  .arms-grid{display:grid; gap:14px;
    grid-template-columns:repeat(auto-fit,minmax(220px,1fr))}
  .arm{position:relative; background:var(--card); border:1px solid var(--line);
    border-radius:var(--radius); padding:17px 17px 15px; box-shadow:var(--shadow);
    overflow:hidden; transition:transform .14s ease, box-shadow .14s ease}
  .arm:hover{transform:translateY(-2px); box-shadow:var(--shadow-lg)}
  .arm .rail{position:absolute; top:0; left:0; right:0; height:4px}
  .arm.best{border-color:color-mix(in srgb,var(--good) 38%, var(--line))}
  .arm-head{display:flex; align-items:center; justify-content:space-between;
    gap:8px; margin-top:5px}
  .arm-name{font-family:var(--mono); font-size:13px; font-weight:600;
    color:var(--ink); overflow:hidden; text-overflow:ellipsis; white-space:nowrap}
  .role{font-size:10px; font-weight:680; letter-spacing:.05em;
    text-transform:uppercase; padding:3px 7px; border-radius:6px;
    color:var(--muted); background:var(--pill-grey-bg);
    border:1px solid var(--line-2); white-space:nowrap}
  .best-ribbon{display:inline-flex; align-items:center; gap:5px; font-size:10px;
    font-weight:700; letter-spacing:.04em; color:var(--good);
    background:var(--good-bg); border:1px solid var(--good-line);
    border-radius:6px; padding:3px 7px; margin-top:10px}
  .arm-cost{display:flex; align-items:baseline; gap:9px; margin-top:11px}
  .arm-cost .v{font-size:27px; font-weight:720; letter-spacing:-.02em;
    line-height:1}
  .arm-cost .lbl{font-size:11px; color:var(--muted)}
  .arm-delta{font-size:12px; font-weight:640; margin-top:5px}
  .arm-delta.good{color:var(--good)} .arm-delta.bad{color:var(--bad)}
  .arm-delta.flat{color:var(--muted)}
  .arm-stats{display:grid; grid-template-columns:1fr 1fr; gap:9px;
    margin-top:14px; padding-top:13px; border-top:1px solid var(--line)}
  .stat .sl{font-size:10.5px; color:var(--muted); letter-spacing:.02em}
  .stat .sv{font-size:14px; font-weight:650; margin-top:2px}
  .arm-foot{display:flex; align-items:center; gap:7px; margin-top:13px;
    font-size:11.5px; color:var(--muted)}
  .arm-foot .ico{flex:0 0 auto}

  /* ---------- charts ---------- */
  .panel{padding:18px 20px 20px}
  .toolbar{display:flex; align-items:center; gap:14px; flex-wrap:wrap;
    margin-bottom:6px}
  .toolbar .field{display:flex; align-items:center; gap:8px}
  .toolbar label{font-size:12px; font-weight:600; color:var(--ink-2)}
  select#metric{font-family:var(--sans); font-size:13px; font-weight:600;
    color:var(--ink); background:var(--card); border:1px solid var(--line-2);
    border-radius:9px; padding:7px 30px 7px 11px; box-shadow:var(--shadow-sm);
    cursor:pointer; appearance:none; -webkit-appearance:none;
    background-image:
      linear-gradient(45deg,transparent 50%,var(--muted) 50%),
      linear-gradient(135deg,var(--muted) 50%,transparent 50%);
    background-position:calc(100% - 16px) 52%, calc(100% - 11px) 52%;
    background-size:5px 5px,5px 5px; background-repeat:no-repeat}
  select#metric:focus{outline:none; border-color:#1f77b4;
    box-shadow:0 0 0 3px color-mix(in srgb,#1f77b4 22%, transparent)}
  .dir-badge{font-size:11px; font-weight:600; color:var(--muted);
    background:var(--card-2); border:1px solid var(--line); border-radius:7px;
    padding:5px 9px}
  .legend{display:flex; gap:14px; flex-wrap:wrap; margin-left:auto}
  .lg{display:inline-flex; align-items:center; gap:7px; font-size:12px;
    color:var(--ink-2)}
  .lg .sw{width:11px; height:11px; border-radius:3px; flex:0 0 auto}
  .coverage{font-size:12px; color:var(--muted); margin:2px 2px 16px;
    display:flex; gap:7px; align-items:center}
  .coverage .warn{color:var(--bad)}

  .charts{display:grid; gap:16px}
  .chart-card{padding:16px 18px 14px}
  .chart-title{font-size:13.5px; font-weight:650; margin-bottom:2px}
  .chart-sub{font-size:11.5px; color:var(--muted); margin-bottom:8px}
  .chart svg{display:block; width:100%; height:auto}
  .chart .hit{cursor:default}
  .chart .hit rect.bar, .chart .hit circle, .chart .hit line.ci{
    transition:opacity .12s ease}
  .chart .hit:hover rect.bar{filter:brightness(1.06)}
  .chart text{font-family:var(--sans)}
  .gtxt{fill:var(--muted); font-size:11px}
  .gtxt-strong{fill:var(--ink-2); font-size:11.5px; font-weight:600}
  .vlabel{fill:var(--ink); font-size:12px; font-weight:680;
    font-variant-numeric:tabular-nums}
  .gridline{stroke:var(--grid); stroke-width:1}
  .axisline{stroke:var(--axis); stroke-width:1}
  .zeroline{stroke:var(--axis); stroke-width:1.4; stroke-dasharray:4 4}
  .empty-chart{display:grid; place-items:center; height:160px;
    color:var(--muted); font-size:13px; gap:8px; text-align:center}

  /* two-up grid for forest+strip on wide screens */
  @media (min-width:760px){
    .charts{grid-template-columns:1fr 1fr}
    .charts .full{grid-column:1 / -1}
  }

  /* ---------- judge ---------- */
  .judge-banner{display:flex; gap:11px; align-items:flex-start;
    background:var(--card-2); border:1px solid var(--line); border-radius:11px;
    padding:12px 14px; margin-bottom:14px}
  .judge-banner .b-ico{flex:0 0 auto; margin-top:1px}
  .judge-banner .tag{font-size:10px; font-weight:740; letter-spacing:.07em;
    color:var(--pill-grey-ink); background:var(--pill-grey-bg);
    border:1px solid var(--line-2); border-radius:6px; padding:2px 7px;
    margin-right:8px}
  .judge-banner p{margin:0; font-size:12.5px; color:var(--ink-2); line-height:1.55}
  .judge-banner p b{color:var(--ink)}
  .jrow{display:grid; grid-template-columns:190px 1fr 168px; gap:14px;
    align-items:center; padding:13px 2px; border-top:1px solid var(--line)}
  .jrow:first-of-type{border-top:none}
  .jlabel{font-size:12px; min-width:0}
  .jlabel .pair{display:flex; align-items:center; gap:6px; flex-wrap:wrap}
  .jlabel .nm{font-family:var(--mono); font-size:11.5px; color:var(--ink-2)}
  .jlabel .vs{color:var(--faint); font-size:11px}
  .jbar{position:relative}
  .jmeta{text-align:right; font-size:11.5px; color:var(--muted)}
  .jmeta .wr{font-size:14px; font-weight:680; color:var(--ink-2)}
  .noise-badge{display:inline-flex; align-items:center; gap:5px; font-size:10px;
    font-weight:700; letter-spacing:.03em; color:var(--bad);
    background:var(--bad-bg); border:1px solid var(--bad-line);
    border-radius:5px; padding:2px 6px}

  /* ---------- details ---------- */
  details.det{margin-top:14px; background:var(--card); border:1px solid var(--line);
    border-radius:13px; box-shadow:var(--shadow-sm); overflow:hidden}
  details.det > summary{cursor:pointer; list-style:none; padding:14px 18px;
    font-size:13.5px; font-weight:640; display:flex; align-items:center; gap:10px;
    user-select:none}
  details.det > summary::-webkit-details-marker{display:none}
  details.det > summary .caret{transition:transform .15s ease; color:var(--muted)}
  details.det[open] > summary .caret{transform:rotate(90deg)}
  details.det > summary .count{margin-left:auto; font-size:11.5px; font-weight:550;
    color:var(--muted)}
  .det-body{padding:2px 18px 18px}
  table.tbl{width:100%; border-collapse:collapse; font-size:12.5px}
  table.tbl th{text-align:right; font-weight:620; color:var(--muted);
    font-size:11px; letter-spacing:.02em; text-transform:uppercase;
    padding:8px 10px; border-bottom:1px solid var(--line-2); white-space:nowrap}
  table.tbl th.l, table.tbl td.l{text-align:left}
  table.tbl td{padding:8px 10px; border-bottom:1px solid var(--line);
    white-space:nowrap; font-variant-numeric:tabular-nums; color:var(--ink-2)}
  table.tbl td.wrap{white-space:normal}
  table.tbl tr:last-child td{border-bottom:none}
  table.tbl td.mono{font-family:var(--mono); font-size:11.5px}
  .pos{color:var(--good)} .neg{color:var(--bad)}
  .badge-sig{font-size:10px; font-weight:700; padding:1px 6px; border-radius:5px}
  .badge-sig.ns{color:var(--muted); background:var(--pill-grey-bg);
    border:1px solid var(--line-2)}
  .badge-sig.sig{color:var(--good); background:var(--good-bg);
    border:1px solid var(--good-line)}
  .swdot{display:inline-block; width:9px; height:9px; border-radius:3px;
    margin-right:6px; vertical-align:-1px}
  .valid-y{color:var(--good); font-weight:640}
  .note{font-size:12px; color:var(--muted); line-height:1.55; margin:6px 2px 0}

  /* ---------- work-product diffs (parsed, plan 024 §1.7) ---------- */
  .cols{display:flex; gap:14px; flex-wrap:wrap}
  .col{flex:1; min-width:240px}
  .col h3{font-family:var(--mono); font-size:12.5px; font-weight:600;
    margin:2px 0 4px}
  .wp-meta{font-size:11.5px; color:var(--muted); margin:8px 0 2px}
  .empty{color:var(--faint); font-style:italic}

  .patch{font-family:var(--mono); font-size:11.5px; line-height:1.5;
    border:1px solid var(--line); border-radius:9px; background:var(--card-2);
    margin:4px 0 8px; overflow:auto; max-height:520px}
  .patch .file{border-top:1px solid var(--line)}
  .patch .file:first-child{border-top:none}
  .file-head{position:sticky; top:0; z-index:2; display:flex; align-items:center;
    gap:8px; padding:6px 10px; background:var(--card);
    border-bottom:1px solid var(--line); font-size:11.5px}
  .file-head .fpath{font-weight:600; color:var(--ink); overflow:hidden;
    text-overflow:ellipsis; white-space:nowrap}
  .file-head .fstat{flex:0 0 auto; font-weight:700; font-size:10px; padding:1px 6px;
    border-radius:5px; border:1px solid var(--line-2); color:var(--muted);
    background:var(--pill-grey-bg)}
  .file-head .fstat.s-A{color:var(--good); background:var(--good-bg);
    border-color:var(--good-line)}
  .file-head .fstat.s-D{color:var(--bad); background:var(--bad-bg);
    border-color:var(--bad-line)}
  .file-head .fcount{margin-left:auto; flex:0 0 auto; color:var(--muted);
    font-variant-numeric:tabular-nums}
  .hunk-head{padding:3px 10px; color:var(--hunk); background:var(--card-2);
    border-bottom:1px solid var(--line); white-space:pre-wrap; word-break:break-all}
  .row{display:grid; grid-template-columns:auto auto 1ch 1fr; align-items:start;
    min-height:1.45em}
  .row .ln{position:sticky; left:0; padding:0 8px; text-align:right;
    color:var(--faint); font-variant-numeric:tabular-nums; min-width:2.4em;
    background:var(--card-2)}
  .row .sg{text-align:center; color:var(--muted)}
  .row .tx{white-space:pre-wrap; overflow-wrap:anywhere; padding-right:8px}
  .ln,.sg{user-select:none}
  .row.add{background:color-mix(in srgb,var(--good) 7%, transparent);
    box-shadow:inset 3px 0 0 var(--good)}
  .row.del{background:color-mix(in srgb,var(--bad) 7%, transparent);
    box-shadow:inset 3px 0 0 var(--bad)}
  .row.add .ln{background:color-mix(in srgb,var(--good) 7%, var(--card-2))}
  .row.del .ln{background:color-mix(in srgb,var(--bad) 7%, var(--card-2))}
  .row.add .tx{color:var(--good)} .row.del .tx{color:var(--bad)}
  .row .w{border-radius:3px; padding:0 1px; font-weight:600}
  .row.add .w{background:var(--w-add)} .row.del .w{background:var(--w-del)}
  .fold{padding:3px 10px; background:var(--card-2);
    border-bottom:1px solid var(--line)}
  .fold > button{font:inherit; color:var(--muted); background:none; border:none;
    cursor:pointer; padding:2px 0}
  .fold > button:hover{color:var(--ink-2); text-decoration:underline}
  .trunc{padding:6px 10px; color:var(--bad); background:var(--bad-bg);
    border-top:1px solid var(--bad-line); font-size:11px}

  /* ---------- interactive .cmp diff shell (plan 024 §1.2/§1.5/§1.7) ------- */
  .cmp{position:relative; margin-top:14px; background:var(--card);
    border:1px solid var(--line); border-radius:13px; box-shadow:var(--shadow-sm);
    overflow:visible}
  .cmp-bar{position:sticky; top:0; z-index:6; display:flex; flex-wrap:wrap;
    align-items:center; gap:8px 12px; padding:10px 12px; background:var(--card);
    border-bottom:1px solid var(--line);
    border-radius:13px 13px 0 0}
  .cmp-task{font-size:13px; font-weight:660; display:flex; align-items:baseline;
    gap:8px}
  .cmp-task .count{font-size:11.5px; font-weight:550; color:var(--muted)}
  .cmp .armrun{display:flex; flex-wrap:wrap; gap:8px}
  .cmp .armpick{display:inline-flex; align-items:center; gap:6px; font-size:11.5px;
    color:var(--ink-2)}
  .cmp .armpick .al{font-family:var(--mono); font-size:11px; color:var(--muted)}
  .cmp select.runsel{font-family:var(--sans); font-size:12px; font-weight:600;
    color:var(--ink); background:var(--card); border:1px solid var(--line-2);
    border-radius:8px; padding:5px 9px; cursor:pointer}
  .cmp select.runsel:disabled{color:var(--faint); cursor:not-allowed}
  .modesw{display:inline-flex; background:var(--card-2); border:1px solid var(--line);
    border-radius:9px; padding:2px}
  .modesw .mtab{font:inherit; font-size:12px; font-weight:600; color:var(--muted);
    background:none; border:none; border-radius:7px; padding:5px 11px; cursor:pointer}
  .modesw .mtab[aria-selected="true"]{color:var(--ink); background:var(--card);
    box-shadow:var(--shadow-sm)}
  .viewsw{display:inline-flex; gap:0}
  .cmp .vbtn{font:inherit; font-size:12px; font-weight:600; color:var(--ink-2);
    background:var(--card); border:1px solid var(--line-2); padding:5px 11px;
    cursor:pointer}
  .viewsw .vbtn:first-child{border-radius:8px 0 0 8px}
  .viewsw .vbtn:last-child{border-radius:0 8px 8px 0; border-left:none}
  .cmp .wrapbtn{border-radius:8px}
  .cmp .vbtn[aria-pressed="true"]{color:var(--ink);
    background:color-mix(in srgb,var(--accent) 12%, var(--card));
    border-color:color-mix(in srgb,var(--accent) 45%, var(--line-2))}
  .cmp .diffsearch{font:inherit; font-size:12px; color:var(--ink);
    background:var(--card); border:1px solid var(--line-2); border-radius:8px;
    padding:5px 10px; min-width:150px; flex:1 1 150px; max-width:280px}
  .filerail{flex:1 1 100%; display:flex; flex-wrap:wrap; gap:5px; align-items:center;
    margin-top:2px}
  .filerail:empty{display:none}
  .filerail .railsel{display:none; font:inherit; font-size:12px; color:var(--ink);
    background:var(--card); border:1px solid var(--line-2); border-radius:8px;
    padding:5px 9px; max-width:100%}
  .railchip{font:inherit; font-size:11px; font-family:var(--mono); color:var(--ink-2);
    background:var(--card-2); border:1px solid var(--line); border-radius:7px;
    padding:3px 8px; cursor:pointer; max-width:240px; overflow:hidden;
    text-overflow:ellipsis; white-space:nowrap}
  .railchip:hover{border-color:var(--line-2); color:var(--ink)}
  .railchip.cur{color:var(--ink); background:var(--rail-cur);
    border-color:color-mix(in srgb,var(--accent) 45%, var(--line))}
  .railchip .rc{color:var(--muted); margin-left:6px; font-variant-numeric:tabular-nums}

  .panes{display:flex; gap:0}
  .pane{flex:1 1 0; min-width:0; max-height:72vh; overflow:auto;
    border-right:1px solid var(--line)}
  .pane:last-child{border-right:none}
  .pane-h{position:sticky; top:0; z-index:3; padding:6px 10px;
    font-family:var(--mono); font-size:11.5px; font-weight:600; color:var(--ink-2);
    background:var(--card-2); border-bottom:1px solid var(--line)}
  .panes[data-mode="focus"] .pane{display:none}
  .panes[data-mode="focus"] .pane.focus{display:block; flex:1 1 100%}
  .diffview[hidden]{display:none}
  /* the pane is the single scroll container: flatten the inner batch-1 .patch box */
  .cmp .pane .patch{max-height:none; overflow:visible; border:none; border-radius:0;
    margin:0; background:transparent}
  .cmp .pane .wp-meta{padding:0 10px}
  .cmp .row .tx{white-space:pre; overflow-wrap:normal}
  .cmp.wrap .row .tx, .cmp.wrap .sp-tx{white-space:pre-wrap; overflow-wrap:anywhere}

  /* split view: a 4-col grid [old# | old code | new# | new code] from clones */
  .splitgrid{display:none; grid-template-columns:auto minmax(0,1fr) auto minmax(0,1fr);
    font-family:var(--mono); font-size:11.5px; line-height:1.5}
  .file.split .hunk > .row, .file.split .hunk > .fold{display:none}
  .file.split .splitgrid{display:grid}
  .splitgrid .sp-ln{padding:0 8px; text-align:right; color:var(--faint);
    font-variant-numeric:tabular-nums; user-select:none; background:var(--card-2)}
  .splitgrid .sp-tx{white-space:pre; overflow-wrap:normal; padding:0 8px}
  .splitgrid .sp-tx.del{background:color-mix(in srgb,var(--bad) 7%, transparent);
    color:var(--bad)}
  .splitgrid .sp-tx.add{background:color-mix(in srgb,var(--good) 7%, transparent);
    color:var(--good)}
  .splitgrid .sp-ln.del{background:color-mix(in srgb,var(--bad) 7%, var(--card-2))}
  .splitgrid .sp-ln.add{background:color-mix(in srgb,var(--good) 7%, var(--card-2))}
  .splitgrid .sp-fold{grid-column:1 / -1; padding:3px 10px; color:var(--muted);
    background:var(--card-2); border-bottom:1px solid var(--line)}

  .file-head .copy, .hunk-head .copy{font:inherit; font-size:10.5px; font-weight:600;
    color:var(--muted); background:var(--card-2); border:1px solid var(--line-2);
    border-radius:6px; padding:2px 7px; cursor:pointer; margin-left:8px}
  .file-head .copy{margin-left:8px}
  .file-head .copy.ok, .hunk-head .copy.ok{color:var(--good);
    border-color:var(--good-line)}
  .hunk-head{display:flex; align-items:center; gap:8px}
  .hunk-head .copy{margin-left:auto}

  mark{background:var(--mark-bg); color:var(--mark-ink); border-radius:2px;
    padding:0 1px}

  .legend{position:fixed; right:16px; bottom:16px; z-index:60; display:none;
    max-width:300px; background:var(--card); color:var(--ink);
    border:1px solid var(--line-2); border-radius:12px; box-shadow:var(--shadow-lg);
    padding:13px 15px; font-size:12px; line-height:1.7}
  .legend.on{display:block}
  .legend h4{margin:0 0 6px; font-size:12.5px; font-weight:680}
  .legend kbd{font-family:var(--mono); font-size:11px; background:var(--card-2);
    border:1px solid var(--line-2); border-radius:5px; padding:1px 5px}
  .legend dl{margin:0; display:grid; grid-template-columns:auto 1fr; gap:4px 10px;
    align-items:baseline}
  .legend dt,.legend dd{margin:0}

  .file.hl{animation:hlflash 1.1s ease}
  @keyframes hlflash{0%{background:var(--rail-cur)} 100%{background:transparent}}

  :where(.cmp button, .cmp select, .cmp input, .railchip, .legend
    button):focus-visible{outline:none;
    box-shadow:0 0 0 3px var(--accent-ring); border-radius:7px}

  @media (max-width:720px){
    .panes{flex-direction:column}
    .pane{max-height:60vh; border-right:none; border-bottom:1px solid var(--line)}
    .filerail .railchip{display:none}
    .filerail .railsel{display:inline-block}
  }
  @media (prefers-reduced-motion:reduce){
    .pane{scroll-behavior:auto}
    .file.hl{animation:none}
  }

  footer{margin-top:34px; text-align:center; color:var(--faint); font-size:11.5px}

  /* ---------- tooltip ---------- */
  #tip{position:fixed; z-index:50; pointer-events:none; opacity:0;
    transform:translateY(2px); transition:opacity .1s ease;
    background:color-mix(in srgb,var(--ink) 92%, #000); color:#fff;
    border-radius:9px; padding:9px 11px; font-size:11.5px; line-height:1.5;
    box-shadow:0 8px 26px rgba(0,0,0,.28); max-width:260px;
    font-variant-numeric:tabular-nums}
  #tip.on{opacity:1}
  #tip .tt-h{font-weight:700; margin-bottom:3px; font-family:var(--mono);
    font-size:11px}
  #tip .tt-r{color:rgba(255,255,255,.82)}
  #tip .tt-r b{color:#fff; font-weight:650}
  @media (prefers-color-scheme:dark){
    #tip{background:#fff; color:#0b0d11; box-shadow:0 8px 26px rgba(0,0,0,.6)}
    #tip .tt-r{color:rgba(11,13,17,.72)} #tip .tt-r b{color:#0b0d11}
  }

  @media (max-width:720px){
    .wrap{padding:22px 15px 56px}
    .hero{grid-template-columns:1fr; gap:20px; padding:22px 20px}
    .chips{justify-content:flex-start}
    .jrow{grid-template-columns:1fr; gap:9px}
    .jmeta{text-align:left}
  }
"""


# Vanilla-JS dashboard renderer (inline SVG, no libraries/CDN -> the report stays a
# single self-contained offline file). Reads the embedded `window.SKILL_AB` blob and
# builds header/hero/arms/charts/judge/audit; every value is derived from the blob so
# it generalises across 2-N arms, tasks and metrics. The heavy, escape-sensitive bits
# (work-product diffs, judge reasons) are rendered server-side by Python and appended
# after #app -- the JS never touches them.
_HTML_SCRIPT = r"""(function(){
  "use strict";
  var D = window.SKILL_AB || {};
  var META = D.meta || {};
  var VERD = D.verdict || {label:"inconclusive", tone:"flat",
    text:"Inconclusive at this scale"};
  var MINUS = "−", MIDDOT = "·";

  var METRIC_LABELS = {
    tests_pass:"Tests pass", lint_pass:"Lint pass", build_pass:"Build pass",
    diff_lines:"Diff size", cost_usd:"Cost per run"
  };

  function el(s){ return document.querySelector(s); }
  function esc(s){
    return String(s).replace(/[&<>"]/g, function(c){
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];
    });
  }
  function clip(s, n){ s = String(s); return s.length > n ? s.slice(0, n-1)+"…" : s; }
  function color(a){ return (D.armColors && D.armColors[a]) || "#8c8c8c"; }
  function isControl(a){
    return META.control ? a === META.control : /control/i.test(a);
  }
  function metricLabel(m){
    if (METRIC_LABELS[m]) return METRIC_LABELS[m];
    return String(m).replace(/_/g," ").replace(/\b\w/g, function(c){
      return c.toUpperCase(); });
  }
  function dirOf(m){ return (D.dir && D.dir[m]) || 1; }
  function hasData(m){
    return (D.arms || []).some(function(a){
      return D.armMeans && D.armMeans[a] && D.armMeans[a][m] != null; });
  }
  function metricsWithData(){ return (D.metrics || []).filter(hasData); }
  function mean(a, m){
    var v = D.armMeans && D.armMeans[a] ? D.armMeans[a][m] : null;
    return v == null ? null : v;
  }
  function runsFor(a){
    return (D.runs || []).filter(function(r){ return r.arm === a; });
  }
  function validCount(a){
    return runsFor(a).filter(function(r){ return r.valid; }).length;
  }

  function fmtVal(m, v, dp){
    if (v == null) return MINUS;
    if (m === "cost_usd") return "$" + v.toFixed(dp == null ? 2 : dp);
    if (m === "diff_lines") return v.toFixed(dp == null ? 1 : dp);
    return (v * 100).toFixed(0) + "%";
  }
  function fmtSigned(m, v, dp){
    if (v == null) return MINUS;
    var s = v >= 0 ? "+" : MINUS, a = Math.abs(v);
    if (m === "cost_usd") return s + "$" + a.toFixed(dp == null ? 3 : dp);
    if (m === "diff_lines") return s + a.toFixed(dp == null ? 1 : dp);
    return s + (a * 100).toFixed(0) + "%";
  }
  function tickFmt(m, v){
    if (m === "cost_usd") return "$" + v.toFixed(2);
    if (m === "diff_lines") return v.toFixed(0);
    return (v * 100).toFixed(0) + "%";
  }
  function quality(m, d){
    if (d === 0 || d == null) return "flat";
    return dirOf(m) * (d > 0 ? 1 : -1) > 0 ? "good" : "bad";
  }
  function qualVar(q){
    return q === "good" ? "var(--good)" : q === "bad" ? "var(--bad)"
      : "var(--neutral)";
  }
  function straddles(c){ return c.lo <= 0 && c.hi >= 0; }

  function niceTicks(min, max, count){
    if (min === max){ min -= 0.5; max += 0.5; }
    var span = max - min, step0 = span / (count || 4);
    var mag = Math.pow(10, Math.floor(Math.log(step0) / Math.LN10));
    var norm = step0 / mag;
    var step = (norm < 1.5 ? 1 : norm < 3 ? 2 : norm < 7 ? 5 : 10) * mag;
    var lo = Math.floor(min / step) * step, hi = Math.ceil(max / step) * step;
    var ticks = [];
    for (var v = lo; v <= hi + step * 0.5; v += step){
      ticks.push(Math.abs(v) < step * 1e-9 ? 0 : +v.toFixed(10));
    }
    return { ticks:ticks, lo:lo, hi:hi };
  }
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

  /* ---------- derived facts ---------- */
  var skillArms = (D.arms || []).filter(function(a){ return !isControl(a); });
  var controlArm = (D.arms || []).filter(isControl)[0] || null;
  var nTasks = (D.tasks || []).length;
  var nRuns = (D.runs || []).length;
  var k = META.k != null ? META.k
    : ((D.arms && D.arms.length && nTasks)
        ? Math.round(nRuns / (D.arms.length * nTasks)) : null);
  var alpha = META.alpha || 0.05;
  var defMetric = metricsWithData()[0] || (D.metrics || [])[0];

  var costArms = (D.arms || []).filter(function(a){
    return mean(a,"cost_usd") != null; });
  var cheapest = costArms.slice().sort(function(a,b){
    return mean(a,"cost_usd") - mean(b,"cost_usd"); })[0];
  var dearest = costArms.slice().sort(function(a,b){
    return mean(b,"cost_usd") - mean(a,"cost_usd"); })[0];
  var pctCheaper = (cheapest && dearest && mean(dearest,"cost_usd"))
    ? Math.round((mean(dearest,"cost_usd") - mean(cheapest,"cost_usd"))
        / mean(dearest,"cost_usd") * 100) : 0;
  var maxCost = costArms.length
    ? Math.max.apply(null, costArms.map(function(a){ return mean(a,"cost_usd"); }))
    : 0;

  /* the single most decisive (pair, metric): a CI that clears 0, biggest relative
     gap. Prefer the PRIMARY pair (the badge's comparison) so the headline tracks the
     pre-registered story; fall back to any pair only if the primary one is a wash. */
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
  /* Headline must match the verdict pill, which is computed on the PRIMARY pair +
     PRIMARY metric. So lead with that when it is decisive; only then widen to other
     metrics of the primary pair, then to any pair. */
  function leadSignal(){
    var comps = D.comparisons || [];
    var prim = comps.filter(function(c){ return c.key === META.primaryPair; });
    return leadIn(prim, D.primary) || leadIn(prim) || leadIn(comps);
  }
  function bhSurvivors(){
    var tests = 0, surv = 0;
    (D.comparisons || []).forEach(function(c){
      metricsWithData().forEach(function(m){
        var d = c.itt && c.itt[m];
        if (!d || m === D.primary) return;
        tests++;
        if (d.q != null && d.q < alpha) surv++;
      });
    });
    return { tests:tests, surv:surv };
  }

  /* ---------- icons ---------- */
  var IC = {
    bolt:"<svg width='15' height='15' viewBox='0 0 24 24' fill='none'>"
      + "<path d='M13 2 4 14h6l-1 8 9-12h-6l1-8z' stroke='currentColor'"
      + " stroke-width='1.7' stroke-linejoin='round'/></svg>",
    minus:"<svg width='15' height='15' viewBox='0 0 24 24' fill='none'>"
      + "<path d='M5 12h14' stroke='currentColor' stroke-width='1.8'"
      + " stroke-linecap='round'/></svg>",
    info:"<svg width='15' height='15' viewBox='0 0 24 24' fill='none'>"
      + "<circle cx='12' cy='12' r='9' stroke='currentColor' stroke-width='1.6'/>"
      + "<path d='M12 11v5M12 8h.01' stroke='currentColor' stroke-width='1.7'"
      + " stroke-linecap='round'/></svg>",
    caret:"<svg width='13' height='13' viewBox='0 0 24 24' fill='none'>"
      + "<path d='M9 6l6 6-6 6' stroke='currentColor' stroke-width='2'"
      + " stroke-linecap='round' stroke-linejoin='round'/></svg>",
    spark:"<svg width='17' height='17' viewBox='0 0 24 24' fill='none'>"
      + "<path d='M12 3v4M12 17v4M3 12h4M17 12h4M6 6l2.5 2.5M15.5 15.5 18 18"
      + "M18 6l-2.5 2.5M8.5 15.5 6 18' stroke='#fff' stroke-width='1.7'"
      + " stroke-linecap='round'/></svg>"
  };

  /* ======================================================================
     HEADER
     ====================================================================== */
  function header(){
    var titleHtml = (skillArms.length ? skillArms : (D.arms || []))
      .map(esc).join("<span class='vs'>vs</span>");
    var coverage = metricsWithData().length, total = (D.metrics || []).length;
    var primaryHasData = hasData(D.primary);
    var chips = [
      ["<span class='dot' style='background:#1f77b4'></span>", "model",
        esc(META.model || "?")],
      ["", "CLI", esc(META.cli || "?")],
      ["", "", nRuns + " runs " + MIDDOT + " " + nTasks + " task"
        + (nTasks === 1 ? "" : "s") + (k != null ? " " + MIDDOT + " k=" + k : "")],
      ["", "seed", esc(String(META.seed != null ? META.seed : 0))],
      ["", "estimand", esc(META.estimand || "ITT")],
      ["", "primary", esc(D.primary || "?")
        + (primaryHasData ? "" : " (no data)")]
    ];
    var chipHtml = chips.map(function(c){
      var key = c[1] ? "<span class='k'>" + esc(c[1]) + "</span> " : "";
      return "<span class='chip'>" + (c[0] || "") + key + "<b>" + c[2]
        + "</b></span>";
    }).join("");
    return ""
      + "<div class='topbar'><div>"
      +   "<div class='brand'><div class='glyph'>" + IC.spark + "</div><div>"
      +     "<div class='eyebrow'>Skill A/B " + MIDDOT + " coding outcomes</div>"
      +     "<h1 class='title'>" + titleHtml + "</h1></div></div>"
      +   "<div class='subtitle'>Effect of installing a skill, measured against a "
      +     (controlArm ? "no-skill <b>" + esc(controlArm) + "</b> baseline"
        : "baseline")
      +     ". " + coverage + " of " + total + " metrics carried data this run.</div>"
      + "</div><div class='chips'>" + chipHtml + "</div></div>";
  }

  /* ======================================================================
     VERDICT HERO
     ====================================================================== */
  function crossRunnerBanner(){
    /* The primary pair ran under different agent CLIs -- a confounded comparison
       that the verdict downgrades to 'suggestive'. Spell out the confound and which
       CLI each arm used so the grey pill isn't read as "no difference". */
    if (!VERD.crossRunner) return "";
    var runners = META.armRunners || {};
    var chips = Object.keys(runners).map(function(a){
      return "<span class='xrchip'>" + esc(a) + "<span class='arrow'>" + MIDDOT
        + "</span>" + esc(runners[a]) + "</span>";
    }).join("");
    return "<div class='xrun'><b>Confounded — different agent CLIs.</b> "
      + "Any gap bundles the CLI binary, its default model, prompt handling and a "
      + "separate login &amp; billing together with any skill effect, so it can't be a "
      + "controlled verdict — read it as <b>suggestive</b>. The blind-judge panel "
      + "below compares the actual code output CLI-agnostically."
      + (chips ? "<div class='xrchips'>" + chips + "</div>" : "") + "</div>";
  }

  function hero(){
    var lead = leadSignal();
    // pill is the source of truth; a cross-runner pair is never "settled" (its
    // verdict is downgraded to suggestive), so it skips the confident headline.
    var settled = VERD.label !== "inconclusive" && !VERD.crossRunner;
    var ledCol = qualVar(VERD.tone === "good" ? "good"
      : VERD.tone === "bad" ? "bad" : "flat");
    var headline, sentence;
    if (VERD.crossRunner){
      /* Confounded by design -- frame the leading CLI as a lead, never a verdict. */
      if (lead){
        var xa = "<span class='accent' style='color:" + color(lead.c.a) + "'>"
          + esc(lead.c.a) + "</span>";
        headline = xa + " leads on " + esc(metricLabel(lead.m))
          + ", but it's a cross-CLI comparison";
      } else {
        headline = "Cross-CLI comparison — no metric separated the agents";
      }
      sentence = "These arms ran under <b>different agent CLIs</b>, so any gap is "
        + "confounded; see the note below. ";
    } else if (lead && settled){
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

    var judgeSentence = "";
    if ((D.judge || []).length){
      var cons = D.judge.map(function(j){ return j.consistency; });
      var minc = Math.min.apply(null, cons);
      judgeSentence = minc <= 50
        ? "The blind judge <b>flipped across orderings</b> (" + minc
          + "% consistency ≈ a coin flip). "
        : "The blind judge held up across orderings (" + minc
          + "% consistency). ";
    }
    var smoke = nTasks < 2
      ? "With " + nTasks + " task" + (k != null ? " and k=" + k : "")
        + ", read this as a <b>smoke test, not a verdict</b>."
      : "";

    var bh = bhSurvivors();
    var tags = [];
    if (nTasks < 2) tags.push("n_tasks=" + nTasks + " " + MIDDOT
      + " cluster CIs degenerate");
    tags.push(esc(META.estimand || "ITT") + " estimand");
    if (bh.tests) tags.push(bh.surv + " / " + bh.tests + " survive BH");

    var costcard = "";
    if (costArms.length){
      var bars = costArms.map(function(a){
        var v = mean(a, "cost_usd"), w = (v / (maxCost || 1) * 100).toFixed(1);
        var best = a === cheapest;
        return "<div class='mbrow'><div class='mbtop'>"
          + "<span class='nm'><span class='sw' style='background:" + color(a)
          + "'></span><span class='mono'>" + esc(a) + "</span>"
          + (best ? " <span class='mbtag'>cheapest</span>" : "") + "</span>"
          + "<span class='vv num'>" + fmtVal("cost_usd", v, 4) + "</span></div>"
          + "<div class='mbtrack'><div class='mbfill' style='width:" + w
          + "%;background:" + color(a) + "'></div></div></div>";
      }).join("");
      var deltaChip = (cheapest !== dearest)
        ? "<span class='delta-chip good'>" + MINUS + pctCheaper + "% vs "
          + esc(dearest) + "</span>" : "";
      costcard = "<div class='costcard'>"
        + "<div class='cap'>Mean cost " + MIDDOT + " " + esc(cheapest) + "</div>"
        + "<div class='bignum'><span class='v num'>"
        + fmtVal("cost_usd", mean(cheapest,"cost_usd")) + "</span>" + deltaChip
        + "</div><div class='bignum-sub'>per-run average across "
        + validCount(cheapest) + " valid runs</div>"
        + "<div class='minibars'>" + bars + "</div></div>";
    }

    return "<div class='card hero'><div class='hero-l'>"
      + "<span class='status-pill'><span class='led' style='background:" + ledCol
      + ";box-shadow:0 0 0 4px color-mix(in srgb," + ledCol
      + " 22%,transparent)'></span>" + esc(VERD.text) + "</span>"
      + "<h1>" + headline + "</h1>"
      + "<p class='lede'>" + sentence + judgeSentence + smoke + "</p>"
      + crossRunnerBanner()
      + "<div class='hero-tags'>"
      + tags.map(function(t){ return "<span class='htag'>" + t + "</span>"; }).join("")
      + "</div></div>" + costcard + "</div>";
  }

  /* ======================================================================
     ARMS AT A GLANCE
     ====================================================================== */
  function armCard(a){
    var c = color(a), ctrl = isControl(a), best = a === cheapest && costArms.length > 1;
    var cost = mean(a, "cost_usd"), diff = mean(a, "diff_lines");
    var baseCost = controlArm ? mean(controlArm, "cost_usd") : null;
    var deltaHtml = "";
    if (!ctrl && baseCost != null && cost != null){
      var d = cost - baseCost, q = quality("cost_usd", d);
      deltaHtml = "<div class='arm-delta " + q + "'>" + fmtSigned("cost_usd", d)
        + " vs control</div>";
    } else if (ctrl){
      deltaHtml = "<div class='arm-delta flat'>baseline</div>";
    }
    var rs = runsFor(a), actHtml;
    if (ctrl){
      actHtml = IC.minus + "<span>no skill (control)</span>";
    } else {
      var known = rs.filter(function(r){ return r.activated != null; });
      if (known.length){
        var n = rs.filter(function(r){ return r.activated; }).length;
        actHtml = IC.bolt + "<span>" + n + "/" + rs.length + " runs activated</span>";
      } else {
        actHtml = IC.info + "<span>activation not recorded</span>";
      }
    }
    var diffStat = diff == null ? MINUS
      : fmtVal("diff_lines", diff) + " <span style='font-weight:500;"
        + "color:var(--muted);font-size:11px'>lines</span>";
    return "<div class='arm" + (best ? " best" : "") + "'>"
      + "<div class='rail' style='background:linear-gradient(90deg," + c
      + ",color-mix(in srgb," + c + " 55%, transparent))'></div>"
      + "<div class='arm-head'><span class='arm-name'>" + esc(a) + "</span>"
      + "<span class='role'>" + (ctrl ? "control" : "skill") + "</span></div>"
      + (best ? "<div class='best-ribbon'>" + IC.bolt + " Cheapest arm</div>" : "")
      + "<div class='arm-cost'><span class='v num' style='color:"
      + (best ? "var(--good)" : "var(--ink)") + "'>" + fmtVal("cost_usd", cost)
      + "</span><span class='lbl'>mean cost</span></div>" + deltaHtml
      + "<div class='arm-stats'>"
      + "<div class='stat'><div class='sl'>Diff size</div>"
      + "<div class='sv num'>" + diffStat + "</div></div>"
      + "<div class='stat'><div class='sl'>Valid runs</div>"
      + "<div class='sv num'>" + validCount(a) + " / " + rs.length + "</div></div>"
      + "</div><div class='arm-foot'>" + actHtml + "</div></div>";
  }
  function armsSection(){
    return "<div class='section-h'><h2>Arms at a glance</h2>"
      + "<span class='hint'>accent = arm color " + MIDDOT
      + " green = cheaper than control</span></div><div class='arms-grid'>"
      + (D.arms || []).map(armCard).join("") + "</div>";
  }

  /* ======================================================================
     CHART TOOLBAR
     ====================================================================== */
  function toolbar(){
    var opts = (D.metrics || []).map(function(m){
      var dis = hasData(m) ? "" : " disabled";
      var sel = m === defMetric ? " selected" : "";
      var sfx = hasData(m) ? "" : " " + MIDDOT + " no data";
      return "<option value='" + esc(m) + "'" + dis + sel + ">"
        + esc(metricLabel(m)) + sfx + "</option>";
    }).join("");
    var legend = (D.arms || []).map(function(a){
      return "<span class='lg'><span class='sw' style='background:" + color(a)
        + "'></span>" + esc(a) + "</span>";
    }).join("");
    var empties = (D.metrics || []).filter(function(m){ return !hasData(m); })
      .map(metricLabel);
    var cov = empties.length
      ? "<span class='warn'>" + IC.info + "</span> " + empties.length
        + " metric" + (empties.length === 1 ? "" : "s")
        + " had no data this run (" + empties.join(", ")
        + ") " + MIDDOT + " showing <b style='color:var(--ink-2)'>"
        + esc(metricLabel(defMetric)) + "</b>"
      : "All metrics carried data.";
    return "<div class='section-h'><h2>Per-metric breakdown</h2>"
      + "<span class='hint'>drives all three charts " + MIDDOT
      + " hover any mark</span></div><div class='card panel'><div class='toolbar'>"
      + "<div class='field'><label for='metric'>Metric</label>"
      + "<select id='metric'>" + opts + "</select></div>"
      + "<span class='dir-badge' id='dirBadge'></span>"
      + "<div class='legend'>" + legend + "</div></div>"
      + "<div class='coverage'>" + cov + "</div>"
      + "<div class='charts' id='charts'></div></div>";
  }

  function chartCard(title, sub, svg, full){
    return "<div class='card chart-card chart" + (full ? " full" : "") + "'>"
      + "<div class='chart-title'>" + title + "</div>"
      + "<div class='chart-sub'>" + sub + "</div>" + svg + "</div>";
  }
  function emptyChart(){
    return "<div class='card chart-card chart'><div class='empty-chart'>" + IC.info
      + "<div>No data for this metric in this run.</div></div></div>";
  }

  /* CHART 1 -- per-arm mean bars */
  function barChart(m){
    var arms = (D.arms || []).filter(function(a){ return mean(a, m) != null; });
    if (!arms.length) return emptyChart();
    var W = 640, H = 320, padL = 56, padR = 18, padT = 22, padB = 64;
    var pw = W - padL - padR, ph = H - padT - padB;
    var vals = arms.map(function(a){ return mean(a, m); });
    var nt = niceTicks(0, Math.max.apply(null, vals), 4), yMax = nt.hi || 1;
    function y(v){ return padT + ph - (v / yMax) * ph; }
    var band = pw / arms.length, bw = Math.min(96, band * 0.54);
    var grid = nt.ticks.map(function(t){
      var yy = y(t);
      return "<line class='gridline' x1='" + padL + "' y1='" + yy.toFixed(1)
        + "' x2='" + (W - padR) + "' y2='" + yy.toFixed(1) + "'/>"
        + "<text class='gtxt' x='" + (padL - 9) + "' y='" + (yy + 4).toFixed(1)
        + "' text-anchor='end'>" + tickFmt(m, t) + "</text>";
    }).join("");
    var bars = arms.map(function(a, i){
      var v = mean(a, m), cx = padL + band * i + band / 2;
      var x = cx - bw / 2, yy = y(v), h = padT + ph - yy;
      var t = tip(a, [
        [metricLabel(m) + ":", fmtVal(m, v, m === "cost_usd" ? 4 : 1)],
        ["valid runs:", validCount(a) + " / " + runsFor(a).length],
        ["direction:", dirOf(m) < 0 ? "lower is better" : "higher is better"]
      ]);
      return "<g class='hit' data-tip=\"" + t + "\">"
        + "<rect x='" + (padL + band * i + 1) + "' y='" + padT + "' width='"
        + (band - 2) + "' height='" + ph + "' fill='transparent'/>"
        + "<rect class='bar' x='" + x.toFixed(1) + "' y='" + yy.toFixed(1)
        + "' width='" + bw.toFixed(1) + "' height='" + Math.max(0,h).toFixed(1)
        + "' rx='5' fill='" + color(a) + "'/>"
        + "<rect x='" + x.toFixed(1) + "' y='" + yy.toFixed(1) + "' width='"
        + bw.toFixed(1) + "' height='" + Math.min(6,Math.max(0,h)).toFixed(1)
        + "' rx='5' fill='#fff' opacity='.14'/>"
        + "<text class='vlabel' x='" + cx.toFixed(1) + "' y='" + (yy - 9).toFixed(1)
        + "' text-anchor='middle'>" + fmtVal(m, v) + "</text>"
        + "<text class='gtxt-strong' x='" + cx.toFixed(1) + "' y='" + (padT + ph + 20)
        + "' text-anchor='middle'>" + esc(clip(a, 16)) + "</text>"
        + "<text class='gtxt' x='" + cx.toFixed(1) + "' y='" + (padT + ph + 36)
        + "' text-anchor='middle'>" + (isControl(a) ? "control" : "skill")
        + "</text></g>";
    }).join("");
    var svg = "<svg viewBox='0 0 " + W + " " + H + "' role='img'>" + grid
      + "<line class='axisline' x1='" + padL + "' y1='" + padT + "' x2='" + padL
      + "' y2='" + (padT + ph) + "'/><line class='axisline' x1='" + padL + "' y1='"
      + (padT + ph) + "' x2='" + (W - padR) + "' y2='" + (padT + ph) + "'/>"
      + bars + "</svg>";
    return chartCard("Per-arm mean " + MIDDOT + " " + metricLabel(m),
      "bars start at zero " + MIDDOT + " hover for run counts", svg, false);
  }

  /* CHART 2 -- pairwise effect (forest) */
  function forestChart(m){
    var comps = (D.comparisons || []).filter(function(c){
      return c.itt && c.itt[m]; });
    if (!comps.length) return emptyChart();
    var rowH = 52, W = 640, padL = 16, padR = 24, padT = 14, gut = 168, axisB = 40;
    var H = padT + comps.length * rowH + axisB;
    var px0 = padL + gut, pw = W - padR - px0;
    var lo = Math.min.apply(null, comps.map(function(c){ return c.itt[m].lo; })
      .concat([0]));
    var hi = Math.max.apply(null, comps.map(function(c){ return c.itt[m].hi; })
      .concat([0]));
    var nt = niceTicks(lo, hi, 4);
    function x(v){ return px0 + (v - nt.lo) / ((nt.hi - nt.lo) || 1) * pw; }
    var grid = nt.ticks.map(function(t){
      var xx = x(t);
      return "<line class='" + (t === 0 ? "zeroline" : "gridline") + "' x1='"
        + xx.toFixed(1) + "' y1='" + padT + "' x2='" + xx.toFixed(1) + "' y2='"
        + (padT + comps.length * rowH) + "'/><text class='gtxt' x='" + xx.toFixed(1)
        + "' y='" + (padT + comps.length * rowH + 16) + "' text-anchor='middle'>"
        + tickFmt(m, t) + "</text>";
    }).join("");
    var rows = comps.map(function(c, i){
      var d = c.itt[m], cy = padT + rowH * i + rowH / 2;
      var ns = straddles(d), q = quality(m, d.point);
      var col = ns ? "var(--neutral)" : qualVar(q);
      var xl = x(d.lo), xh = x(d.hi), xp = x(d.point), op = ns ? ".55" : ".9";
      var t = tip(c.a + " " + MINUS + " " + c.b, [
        ["Δ " + metricLabel(m) + ":", fmtSigned(m, d.point, m==="cost_usd"?4:1)],
        ["95% CI:", "[" + fmtVal(m, d.lo, m==="cost_usd"?3:1) + ", "
          + fmtVal(m, d.hi, m==="cost_usd"?3:1) + "]"],
        ["p / q:", (d.p == null ? "—" : d.p.toFixed(3)) + " / "
          + (d.q == null ? "—" : d.q.toFixed(3))],
        ["verdict:", ns ? "CI crosses 0 (n.s.)"
          : (q === "good" ? "favors " + c.a : "favors " + c.b)]
      ]);
      return "<g class='hit' data-tip=\"" + t + "\">"
        + "<rect x='" + padL + "' y='" + (padT + rowH * i) + "' width='"
        + (W - padL - padR) + "' height='" + rowH + "' fill='transparent'/>"
        + "<text class='gtxt-strong' x='" + padL + "' y='" + (cy - 5) + "'>"
        + "<tspan fill='" + color(c.a) + "'>●</tspan> " + esc(clip(c.a, 19))
        + "</text><text class='gtxt' x='" + (padL + 12) + "' y='" + (cy + 11) + "'>"
        + "vs <tspan fill='" + color(c.b) + "'>●</tspan> " + esc(clip(c.b, 19))
        + "</text><line class='ci' x1='" + xl.toFixed(1) + "' y1='" + cy + "' x2='"
        + xh.toFixed(1) + "' y2='" + cy + "' stroke='" + col + "' stroke-width='2.4'"
        + " stroke-linecap='round' opacity='" + op + "'/>"
        + "<line x1='" + xl.toFixed(1) + "' y1='" + (cy-5) + "' x2='" + xl.toFixed(1)
        + "' y2='" + (cy+5) + "' stroke='" + col + "' stroke-width='1.6' opacity='"
        + op + "'/><line x1='" + xh.toFixed(1) + "' y1='" + (cy-5) + "' x2='"
        + xh.toFixed(1) + "' y2='" + (cy+5) + "' stroke='" + col
        + "' stroke-width='1.6' opacity='" + op + "'/>"
        + "<circle cx='" + xp.toFixed(1) + "' cy='" + cy + "' r='5.5' fill='" + col
        + "' stroke='var(--card)' stroke-width='1.6'/><text class='vlabel' x='"
        + xp.toFixed(1) + "' y='" + (cy - 11) + "' text-anchor='middle' fill='" + col
        + "'>" + fmtSigned(m, d.point, m === "cost_usd" ? 3 : 1) + "</text></g>";
    }).join("");
    return chartCard("Pairwise effect " + MIDDOT + " Δ with 95% CI",
      "dashed line = no effect " + MIDDOT
      + " green favors A, red favors B, grey n.s.",
      "<svg viewBox='0 0 " + W + " " + H + "' role='img'>" + grid + rows + "</svg>",
      false);
  }

  /* CHART 3 -- per-run distribution (strip) */
  function stripChart(m){
    var arms = (D.arms || []).filter(function(a){
      return runsFor(a).some(function(r){ return r.scores[m] != null; }); });
    if (!arms.length) return emptyChart();
    var W = 640, H = 320, padL = 56, padR = 18, padT = 20, padB = 56;
    var pw = W - padL - padR, ph = H - padT - padB, all = [];
    arms.forEach(function(a){
      runsFor(a).forEach(function(r){
        if (r.scores[m] != null) all.push(r.scores[m]); }); });
    var mn = Math.min.apply(null, all), mx = Math.max.apply(null, all);
    var pad = (mx - mn) * 0.18 || 1, nt = niceTicks(mn - pad, mx + pad, 4);
    function y(v){ return padT + ph - (v - nt.lo) / ((nt.hi - nt.lo) || 1) * ph; }
    var band = pw / arms.length;
    var grid = nt.ticks.map(function(t){
      var yy = y(t);
      return "<line class='gridline' x1='" + padL + "' y1='" + yy.toFixed(1)
        + "' x2='" + (W - padR) + "' y2='" + yy.toFixed(1) + "'/><text class='gtxt'"
        + " x='" + (padL - 9) + "' y='" + (yy + 4).toFixed(1)
        + "' text-anchor='end'>" + tickFmt(m, t) + "</text>";
    }).join("");
    var cells = arms.map(function(a, i){
      var cx = padL + band * i + band / 2, c = color(a);
      var rs = runsFor(a).filter(function(r){ return r.scores[m] != null; });
      var mv = mean(a, m), spacing = Math.min(22, band / (rs.length + 1));
      var dots = rs.map(function(r, j){
        var jx = cx + (j - (rs.length - 1) / 2) * spacing, jy = y(r.scores[m]);
        var t = tip(a + " " + MIDDOT + " run #" + r.idx, [
          [metricLabel(m) + ":", fmtVal(m, r.scores[m], m==="cost_usd"?4:1)],
          ["cost:", "$" + (r.cost == null ? "?" : r.cost.toFixed(4))],
          ["turns:", String(r.turns == null ? "?" : r.turns)],
          ["valid:", r.valid ? "yes" : "no"]
        ]);
        return "<circle class='hit' data-tip=\"" + t + "\" cx='" + jx.toFixed(1)
          + "' cy='" + jy.toFixed(1) + "' r='6' fill='" + c
          + "' fill-opacity='.82' stroke='var(--card)' stroke-width='1.6'/>";
      }).join("");
      // No ITT-valid runs -> mean is null; skip the marker instead of drawing a
      // bogus one at 0 with a "—" label (barChart drops such arms entirely).
      var meanMark = "";
      if (mv != null) {
        var my = y(mv);
        meanMark = "<line x1='" + (cx - band * 0.32) + "' y1='" + my.toFixed(1)
          + "' x2='" + (cx + band * 0.32) + "' y2='" + my.toFixed(1) + "' stroke='" + c
          + "' stroke-width='2.4'/><text class='gtxt-strong' x='"
          + (cx + band * 0.32 + 4) + "' y='" + (my + 4).toFixed(1) + "' fill='" + c
          + "'>" + fmtVal(m, mv) + "</text>";
      }
      return "<text class='gtxt-strong' x='" + cx.toFixed(1) + "' y='"
        + (padT + ph + 22) + "' text-anchor='middle'>" + esc(clip(a, 16))
        + "</text><text class='gtxt' x='" + cx.toFixed(1) + "' y='" + (padT + ph + 38)
        + "' text-anchor='middle'>" + rs.length + " runs</text>" + meanMark + dots;
    }).join("");
    var svg = "<svg viewBox='0 0 " + W + " " + H + "' role='img'>" + grid
      + "<line class='axisline' x1='" + padL + "' y1='" + padT + "' x2='" + padL
      + "' y2='" + (padT + ph) + "'/><line class='axisline' x1='" + padL + "' y1='"
      + (padT + ph) + "' x2='" + (W - padR) + "' y2='" + (padT + ph) + "'/>" + cells
      + "</svg>";
    return chartCard("Per-run distribution " + MIDDOT + " " + metricLabel(m),
      "one dot per run " + MIDDOT + " bar = arm mean", svg, false);
  }

  /* ======================================================================
     BLIND JUDGE
     ====================================================================== */
  function judgeSection(){
    var J = D.judge || [];
    if (!J.length) return "";
    var W = 440, padL = 8, padR = 8, pw = W - padL - padR;
    function jx(v){ return padL + v * pw; }
    var anyLow = J.some(function(j){ return j.consistency <= 50; });
    var rows = J.map(function(j){
      var ndec = j.win_rate_a == null;          // no decisive verdicts (all ties/failed)
      var wr = ndec ? 0.5 : j.win_rate_a;       // neutral center when undecidable
      var wrStr = ndec ? "—" : wr.toFixed(2);   // em dash placeholder, never null.toFixed
      var low = j.consistency <= 50, favA = wr >= 0.5;
      var c = favA ? color(j.a) : color(j.b);
      var xc = jx(0.5), xv = jx(wr);
      var x0 = Math.min(xc, xv), bw = Math.abs(xv - xc), H = 46, cy = H / 2;
      var ticks = [0, 0.25, 0.5, 0.75, 1].map(function(t){
        var xx = jx(t);
        return "<line x1='" + xx.toFixed(1) + "' y1='6' x2='" + xx.toFixed(1)
          + "' y2='" + (H - 6) + "' stroke='var(--grid)' stroke-width='1'/>";
      }).join("");
      var t = tip(j.a + " vs " + j.b, [
        ["win-rate (A):", wrStr],
        ["95% CI (A):", j.win_rate_a_ci ? "[" + j.win_rate_a_ci[0].toFixed(2)
          + ", " + j.win_rate_a_ci[1].toFixed(2) + "]" : "n/a"],
        ["record:", j.a_wins + "–" + j.b_wins + "–" + j.ties
          + " (A–B–tie)"],
        ["consistency:", j.consistency + "%" + (low ? " (≈ chance)" : "")]
      ]);
      var hatchId = "h_" + String(j.pair).replace(/[^a-z0-9]/gi, "");
      var defs = low ? "<defs><pattern id='" + hatchId
        + "' width='6' height='6' patternUnits='userSpaceOnUse'"
        + " patternTransform='rotate(45)'><rect width='6' height='6' fill='" + c
        + "' fill-opacity='.18'/><line x1='0' y1='0' x2='0' y2='6' stroke='" + c
        + "' stroke-width='2' stroke-opacity='.5'/></pattern></defs>" : "";
      var fill = low ? "url(#" + hatchId + ")" : c;
      var bar = "<g class='hit' data-tip=\"" + t + "\"><rect x='" + padL
        + "' y='6' width='" + pw + "' height='" + (H - 12)
        + "' rx='7' fill='var(--grid)' fill-opacity='.5'/>" + ticks
        + "<rect x='" + x0.toFixed(1) + "' y='9' width='" + Math.max(2,bw).toFixed(1)
        + "' height='" + (H - 18) + "' rx='5' fill='" + fill + "'/><line x1='"
        + xc.toFixed(1) + "' y1='3' x2='" + xc.toFixed(1) + "' y2='" + (H - 3)
        + "' class='zeroline'/><circle cx='" + xv.toFixed(1) + "' cy='" + cy
        + "' r='5' fill='" + c + "' stroke='var(--card)' stroke-width='1.5'"
        + " fill-opacity='" + (low ? ".6" : "1") + "'/></g>";
      var consHtml = low
        ? "<span class='noise-badge'>" + j.consistency + "% " + MIDDOT + " noise</span>"
        : "<span class='num'>" + j.consistency + "% consistent</span>";
      return "<div class='jrow'><div class='jlabel'><div class='pair'>"
        + "<span class='nm'><span class='swdot' style='background:" + color(j.a)
        + "'></span>" + esc(j.a) + "</span></div><div class='pair'>"
        + "<span class='vs'>vs</span><span class='nm'><span class='swdot'"
        + " style='background:" + color(j.b) + "'></span>" + esc(j.b)
        + "</span></div></div><div class='jbar'><svg viewBox='0 0 " + W + " " + H
        + "' role='img'>" + defs + bar + "</svg></div><div class='jmeta'>"
        + "<div class='wr num'>" + wrStr + " <span style='"
        + "font-size:11px;font-weight:500;color:var(--faint)'>win-rate A</span></div>"
        + (j.win_rate_a_ci ? "<div class='num' style='font-size:11px;color:"
          + "var(--faint)'>95% CI [" + j.win_rate_a_ci[0].toFixed(2) + ", "
          + j.win_rate_a_ci[1].toFixed(2) + "]</div>" : "")
        + "<div class='num'>record " + j.a_wins + "–" + j.b_wins
        + (j.ties ? "–" + j.ties : "") + "</div><div style='margin-top:5px'>"
        + consHtml + "</div></div></div>";
    }).join("");
    var bannerLow = anyLow
      ? "Pairs came back at <b>≤50% position-consistency</b> — the judge "
        + "<b>flipped</b> when the diffs were swapped, i.e. it is essentially "
        + "guessing. Low-consistency bars are <b>hatched and discounted</b>; "
        + "<b>not evidence</b> either way."
      : "Each pair was graded blind in both orderings to cancel position bias. "
        + "Win-rate is centered at 0.50.";
    return "<div class='section-h'><h2>Blind judge <span style='font-weight:500;"
      + "color:var(--muted);font-size:12px'>head-to-head</span></h2>"
      + "<span class='hint'>win-rate centered at 0.50</span></div>"
      + "<div class='card panel'><div class='judge-banner'>"
      + "<span class='b-ico' style='color:var(--muted)'>" + IC.info + "</span>"
      + "<p><span class='tag'>NON-DETERMINISTIC</span>" + bannerLow + "</p></div>"
      + rows + "</div>";
  }

  /* ======================================================================
     AUDIT TABLES (safe; no diffs)
     ====================================================================== */
  function comparisonTable(){
    var mWithData = metricsWithData();
    var head = "<tr><th class='l'>Comparison</th><th class='l'>Metric</th>"
      + "<th>Δ (A−B)</th><th>95% CI</th><th>p</th><th>q (BH)</th>"
      + "<th class='l'>Result</th></tr>";
    var body = (D.comparisons || []).map(function(c){
      return mWithData.map(function(m){
        var d = c.itt && c.itt[m]; if (!d) return "";
        var ns = straddles(d), q = quality(m, d.point);
        var cls = ns ? "" : (q === "good" ? "pos" : "neg");
        var sig = ns ? "<span class='badge-sig ns'>n.s.</span>"
          : "<span class='badge-sig sig'>CI excl. 0</span>";
        return "<tr><td class='l mono'>" + esc(c.a) + " vs " + esc(c.b)
          + "</td><td class='l'>" + esc(metricLabel(m)) + "</td><td class='" + cls
          + "'>" + fmtSigned(m, d.point, m==="cost_usd"?4:1) + "</td><td>["
          + fmtVal(m, d.lo, m==="cost_usd"?3:1) + ", "
          + fmtVal(m, d.hi, m==="cost_usd"?3:1) + "]</td><td>"
          + (d.p == null ? "—" : d.p.toFixed(3)) + "</td><td>"
          + (d.q == null ? "—" : d.q.toFixed(3)) + "</td><td class='l'>" + sig
          + "</td></tr>";
      }).join("");
    }).join("");
    var bh = bhSurvivors();
    var note = "<p class='note'>" + IC.info + " Primary endpoint <b>"
      + esc(D.primary) + "</b> is uncorrected; secondary metrics are "
      + "Benjamini–Hochberg corrected — <b>" + bh.surv + " of " + bh.tests
      + "</b> survive at α=" + alpha + "."
      + (nTasks < 2 ? " With <b>n_tasks=1</b> the cluster bootstrap is degenerate, "
        + "so these intervals are anticonservative." : "") + "</p>";
    return "<table class='tbl'><thead>" + head + "</thead><tbody>" + body
      + "</tbody></table>" + note;
  }
  function runsTable(){
    var mWithData = metricsWithData();
    var head = "<tr><th class='l'>Arm</th><th class='l'>Task</th><th>Run</th>"
      + "<th>Valid</th><th>Activated</th><th>Contam</th><th>Turns</th>"
      + mWithData.map(function(m){ return "<th>" + esc(metricLabel(m)) + "</th>"; })
        .join("") + "</tr>";
    var sorted = (D.runs || []).slice().sort(function(a, b){
      var ai = (D.arms || []).indexOf(a.arm), bi = (D.arms || []).indexOf(b.arm);
      return ai - bi || a.idx - b.idx;
    });
    var body = sorted.map(function(r){
      var cells = mWithData.map(function(m){
        return "<td>" + (r.scores[m] == null ? MINUS
          : fmtVal(m, r.scores[m], m==="cost_usd"?4:1)) + "</td>";
      }).join("");
      var act = r.activated == null ? MINUS : (r.activated ? "yes" : "no");
      var contam = r.contam ? "<span class='neg'>" + esc(r.contam) + "</span>"
        : MINUS;
      return "<tr><td class='l mono'><span class='swdot' style='background:"
        + color(r.arm) + "'></span>" + esc(r.arm) + "</td><td class='l'>"
        + esc(r.task) + "</td><td>#" + r.idx + "</td><td>"
        + (r.valid ? "<span class='valid-y'>yes</span>" : "no") + "</td><td>" + act
        + "</td><td>" + contam + "</td><td>" + (r.turns == null ? MINUS : r.turns)
        + "</td>" + cells + "</tr>";
    }).join("");
    return "<table class='tbl'><thead>" + head + "</thead><tbody>" + body
      + "</tbody></table>";
  }
  function detailsSection(){
    return "<div class='section-h'><h2>Audit detail</h2>"
      + "<span class='hint'>every run, nothing dropped</span></div>"
      + "<details class='det'><summary><span class='caret'>" + IC.caret
      + "</span>Pairwise statistics<span class='count'>"
      + (D.comparisons || []).length + " comparisons</span></summary>"
      + "<div class='det-body'>" + comparisonTable() + "</div></details>"
      + "<details class='det'><summary><span class='caret'>" + IC.caret
      + "</span>All runs<span class='count'>" + nRuns + " runs</span></summary>"
      + "<div class='det-body'>" + runsTable() + "</div></details>";
  }

  /* ======================================================================
     RENDER + WIRING
     ====================================================================== */
  function renderCharts(m){
    el("#charts").innerHTML = barChart(m) + forestChart(m) + stripChart(m);
    el("#dirBadge").textContent = dirOf(m) < 0 ? "lower is better"
      : "higher is better";
  }
  function mount(){
    el("#app").innerHTML = header() + hero() + armsSection() + toolbar()
      + judgeSection() + detailsSection();
    renderCharts(defMetric);
    var sel = el("#metric");
    if (sel){ sel.value = defMetric;
      sel.addEventListener("change", function(){ renderCharts(sel.value); }); }
  }
  function wireTooltip(){
    var t = el("#tip");
    document.addEventListener("mouseover", function(e){
      var n = e.target.closest ? e.target.closest("[data-tip]") : null;
      if (n){ t.innerHTML = n.getAttribute("data-tip"); t.classList.add("on"); }
    });
    document.addEventListener("mousemove", function(e){
      if (!t.classList.contains("on")) return;
      var pad = 16, w = t.offsetWidth, h = t.offsetHeight;
      var x = e.clientX + pad, y = e.clientY + pad;
      if (x + w > window.innerWidth - 8) x = e.clientX - w - pad;
      if (y + h > window.innerHeight - 8) y = e.clientY - h - pad;
      t.style.left = Math.max(8, x) + "px"; t.style.top = Math.max(8, y) + "px";
    });
    document.addEventListener("mouseout", function(e){
      var n = e.target.closest ? e.target.closest("[data-tip]") : null;
      if (n) t.classList.remove("on");
    });
  }
  function wireDiff(){
    // Tiny fold-expand toggle for server-rendered diffs: reveal the stashed
    // (already-escaped) context rows, then drop the button. No innerHTML writes.
    document.addEventListener("click", function(e){
      var t = e.target;
      var b = t && t.closest ? t.closest("button[data-act='expand']") : null;
      if (!b) return;
      var fold = b.parentNode;
      var hid = fold ? fold.querySelector(".folded") : null;
      if (hid) hid.hidden = false;
      b.setAttribute("aria-expanded", "true");
      if (fold) fold.removeChild(b);
    });
  }

  /* ======================================================================
     DiffViewer -- interactive .cmp diff shell (plan 024 §1.5). Escape-safe by
     construction: Split CLONES already-escaped .tx nodes, Search wraps matches
     via DOM Range/createElement, Copy reads .textContent, the rail reads
     getAttribute/textContent. No diff-derived byte is ever assigned to
     innerHTML. The data blob carries only ids/counts/flags (D.work), never text.
     ====================================================================== */
  function DiffViewer(){
    var cmps = Array.prototype.slice.call(document.querySelectorAll(".cmp"));
    if (!cmps.length) return;
    var reduceMotion = window.matchMedia
      && window.matchMedia("(prefers-reduced-motion:reduce)").matches;
    var sBehav = reduceMotion ? "auto" : "smooth";
    var legendEl = null;

    function lsGet(k){ try { return window.localStorage.getItem(k); } catch (e) { return null; } }
    function lsSet(k, v){ try { window.localStorage.setItem(k, v); } catch (e) {} }
    function list(root, sel){ return Array.prototype.slice.call(root.querySelectorAll(sel)); }
    function panesOf(cmp){ return list(cmp, ".pane"); }
    function panesEl(cmp){ return cmp.querySelector(".panes"); }
    function modeOf(cmp){ var p = panesEl(cmp); return p ? p.getAttribute("data-mode") : "compare"; }
    function visibleView(pane){ return pane ? pane.querySelector(".diffview:not([hidden])") : null; }
    function focusPane(cmp){ return cmp._focus || panesOf(cmp)[0] || null; }
    function activePane(cmp){
      if (modeOf(cmp) === "focus") return cmp.querySelector(".pane.focus") || panesOf(cmp)[0];
      return focusPane(cmp);
    }
    function curFiles(cmp){
      var v = visibleView(activePane(cmp));
      return v ? list(v, "[id^='f-']") : [];
    }
    function curHunks(cmp){
      var v = visibleView(activePane(cmp));
      return v ? list(v, ".hunk") : [];
    }

    /* ---- scrolling helpers ---- */
    function scrollPaneTo(pane, el){
      if (!pane || !el) return;
      var pr = pane.getBoundingClientRect(), er = el.getBoundingClientRect();
      var head = pane.querySelector(".pane-h");
      var off = head ? head.offsetHeight : 0;
      var top = pane.scrollTop + (er.top - pr.top) - off - 4;
      if (pane.scrollTo) pane.scrollTo({ top: top, behavior: sBehav });
      else pane.scrollTop = top;
    }
    function scrollEnd(pane, bottom){
      if (!pane) return;
      var top = bottom ? pane.scrollHeight : 0;
      if (pane.scrollTo) pane.scrollTo({ top: top, behavior: sBehav });
      else pane.scrollTop = top;
    }

    /* ---- run picker ---- */
    function showRun(cmp, armVal, runId){
      var pane = cmp.querySelector(".pane[data-arm='" + armVal + "']");
      if (!pane) return;
      list(pane, ".diffview").forEach(function(v){
        v.hidden = v.getAttribute("data-run") !== runId;
      });
      clearMarks(cmp);
      refresh(cmp);
    }

    /* ---- mode / focus ---- */
    function setMode(cmp, mode){
      var p = panesEl(cmp);
      if (!p) return;
      p.setAttribute("data-mode", mode);
      list(cmp, ".modesw .mtab").forEach(function(t){
        t.setAttribute("aria-selected", t.getAttribute("data-mode") === mode ? "true" : "false");
      });
      lsSet("skillab:mode:" + cmp.getAttribute("data-task"), mode);
      refresh(cmp);
    }
    function setFocusPane(cmp, pane){
      if (!pane || cmp._focus === pane) return;
      cmp._focus = pane;
      panesOf(cmp).forEach(function(p){ p.classList.toggle("focus", p === pane); });
      refresh(cmp);
    }

    /* ---- wrap ---- */
    function setWrap(cmp, on){
      cmp.classList.toggle("wrap", on);
      var b = cmp.querySelector(".wrapbtn");
      if (b) b.setAttribute("aria-pressed", on ? "true" : "false");
      lsSet("skillab:wrap", on ? "1" : "0");
    }
    function toggleWrap(cmp){ setWrap(cmp, !cmp.classList.contains("wrap")); }

    /* ---- split (per-file, persisted; toolbar bulk-sets the whole cmp) ---- */
    function buildSplit(file){
      if (file._splitBuilt) return;
      list(file, ".hunk").forEach(function(hunk){
        var grid = document.createElement("div");
        grid.className = "splitgrid";
        var pendDel = [], pendAdd = [];
        function lnClone(row, which){
          var lns = row.querySelectorAll(".ln");
          return lns[which] ? lns[which].cloneNode(true) : null;
        }
        function cell(cls, node){
          var c = document.createElement("div");
          c.className = cls;
          if (node) c.appendChild(node);
          else c.appendChild(document.createTextNode(""));
          return c;
        }
        function txClone(row){
          var tx = row.querySelector(".tx");
          return tx ? tx.cloneNode(true) : null;
        }
        function flush(){
          var n = Math.max(pendDel.length, pendAdd.length);
          for (var i = 0; i < n; i++){
            var d = pendDel[i], a = pendAdd[i];
            grid.appendChild(cell("sp-ln" + (d ? " del" : ""), d ? lnClone(d, 0) : null));
            grid.appendChild(cell("sp-tx" + (d ? " del" : ""), d ? txClone(d) : null));
            grid.appendChild(cell("sp-ln" + (a ? " add" : ""), a ? lnClone(a, 1) : null));
            grid.appendChild(cell("sp-tx" + (a ? " add" : ""), a ? txClone(a) : null));
          }
          pendDel = []; pendAdd = [];
        }
        var kids = hunk.children;
        for (var k = 0; k < kids.length; k++){
          var node = kids[k];
          if (node.classList.contains("row")){
            if (node.classList.contains("del")) pendDel.push(node);
            else if (node.classList.contains("add")) pendAdd.push(node);
            else {
              flush();
              grid.appendChild(cell("sp-ln", lnClone(node, 0)));
              grid.appendChild(cell("sp-tx", txClone(node)));
              grid.appendChild(cell("sp-ln", lnClone(node, 1)));
              grid.appendChild(cell("sp-tx", txClone(node)));
            }
          } else if (node.classList.contains("fold")){
            flush();
            var fc = document.createElement("div");
            fc.className = "sp-fold";
            var btn = node.querySelector("button");
            fc.appendChild(document.createTextNode(
              btn ? btn.textContent : (node.getAttribute("data-n") || "") + " unchanged lines"));
            grid.appendChild(fc);
          }
        }
        flush();
        hunk.appendChild(grid);
      });
      file._splitBuilt = true;
    }
    function setFileSplit(file, on){
      if (on) buildSplit(file);
      file.classList.toggle("split", !!on);
      if (file.id) lsSet("skillab:split:" + file.id, on ? "1" : "0");
    }
    function cmpSplitOn(cmp){
      var files = list(cmp, ".file");
      if (!files.length) return false;
      for (var i = 0; i < files.length; i++)
        if (!files[i].classList.contains("split")) return false;
      return true;
    }
    function reflectSplit(cmp){
      var on = cmpSplitOn(cmp);
      list(cmp, ".viewsw .vbtn").forEach(function(b){
        b.setAttribute("aria-pressed",
          ((b.getAttribute("data-act") === "split") === on) ? "true" : "false");
      });
    }
    function setSplit(cmp, on){
      list(cmp, ".file").forEach(function(f){ setFileSplit(f, on); });
      reflectSplit(cmp);
    }
    function toggleSplit(cmp){ setSplit(cmp, !cmpSplitOn(cmp)); }
    function restoreSplit(cmp){
      list(cmp, ".file").forEach(function(f){
        if (f.id && lsGet("skillab:split:" + f.id) === "1") setFileSplit(f, true);
      });
      reflectSplit(cmp);
    }

    /* ---- file rail + scroll-spy ---- */
    function buildRail(cmp){
      var rail = cmp.querySelector(".filerail");
      if (!rail) return;
      while (rail.firstChild) rail.removeChild(rail.firstChild);
      var files = curFiles(cmp);
      if (!files.length) return;
      var sel = document.createElement("select");
      sel.className = "railsel";
      sel.setAttribute("aria-label", "jump to file");
      files.forEach(function(f){
        var path = f.getAttribute("data-path") || f.id;
        var add = f.getAttribute("data-add") || "0", del = f.getAttribute("data-del") || "0";
        var meta = "+" + add + " −" + del;
        var chip = document.createElement("button");
        chip.type = "button"; chip.className = "railchip";
        chip.setAttribute("data-target", f.id);
        chip.appendChild(document.createTextNode(path));
        var rc = document.createElement("span");
        rc.className = "rc"; rc.appendChild(document.createTextNode(meta));
        chip.appendChild(rc);
        chip.addEventListener("click", function(){ gotoFile(cmp, f.id); });
        rail.appendChild(chip);
        var opt = document.createElement("option");
        opt.value = f.id;
        opt.appendChild(document.createTextNode(path + "  " + meta));
        sel.appendChild(opt);
      });
      sel.addEventListener("change", function(){ gotoFile(cmp, sel.value); });
      rail.appendChild(sel);
    }
    function markCur(cmp, id){
      var rail = cmp.querySelector(".filerail");
      if (!rail) return;
      list(rail, ".railchip").forEach(function(c){
        c.classList.toggle("cur", c.getAttribute("data-target") === id);
      });
      var sel = rail.querySelector(".railsel");
      if (sel) sel.value = id;
    }
    function flashFile(el){
      el.classList.remove("hl");
      void el.offsetWidth;
      el.classList.add("hl");
      setTimeout(function(){ el.classList.remove("hl"); }, 1200);
    }
    function gotoFile(cmp, id){
      var target = document.getElementById(id);
      if (!target) return;
      var pane = target.closest ? target.closest(".pane") : null;
      scrollPaneTo(pane, target);
      var path = target.getAttribute("data-path");
      panesOf(cmp).forEach(function(p){
        if (p === pane) return;
        var v = visibleView(p);
        if (!v) return;
        var fs = list(v, "[id^='f-']");
        for (var i = 0; i < fs.length; i++){
          if (fs[i].getAttribute("data-path") === path){ scrollPaneTo(p, fs[i]); break; }
        }
      });
      flashFile(target);
      markCur(cmp, id);
    }
    function spyRewire(cmp){
      if (!window.IntersectionObserver) return;
      if (cmp._io){ cmp._io.disconnect(); cmp._io = null; }
      var pane = activePane(cmp);
      var view = visibleView(pane);
      if (!view) return;
      var io = new IntersectionObserver(function(entries){
        var best = null;
        entries.forEach(function(en){
          if (en.isIntersecting
              && (!best || en.boundingClientRect.top < best.boundingClientRect.top))
            best = en;
        });
        if (best) markCur(cmp, best.target.id);
      }, { root: pane, rootMargin: "0px 0px -70% 0px", threshold: 0 });
      list(view, "[id^='f-']").forEach(function(f){ io.observe(f); });
      cmp._io = io;
    }
    function refresh(cmp){ buildRail(cmp); spyRewire(cmp); }

    /* ---- sync-scroll (Compare), ratio-mirrored + reentrancy guard ---- */
    function syncScroll(cmp){
      var panes = panesOf(cmp);
      if (panes.length < 2) return;
      panes.forEach(function(src){
        src.addEventListener("scroll", function(){
          if (src._suppress){ src._suppress = false; return; }
          if (modeOf(cmp) === "focus") return;
          if (src._raf) return;
          src._raf = requestAnimationFrame(function(){
            src._raf = null;
            var denom = src.scrollHeight - src.clientHeight;
            var ratio = denom > 0 ? src.scrollTop / denom : 0;
            panes.forEach(function(dst){
              if (dst === src) return;
              var top = ratio * (dst.scrollHeight - dst.clientHeight);
              if (Math.abs(dst.scrollTop - top) > 1){ dst._suppress = true; dst.scrollTop = top; }
            });
          });
        });
      });
    }

    /* ---- copy (clipboard API -> hidden-textarea execCommand fallback) ---- */
    function diffText(scope){
      var out = [];
      list(scope, ".row").forEach(function(r){
        var sg = r.querySelector(".sg"), tx = r.querySelector(".tx");
        out.push((sg ? sg.textContent : " ") + (tx ? tx.textContent : ""));
      });
      return out.join("\n");
    }
    function fallbackCopy(text){
      try {
        var ta = document.createElement("textarea");
        ta.value = text; ta.setAttribute("readonly", "");
        ta.style.position = "fixed"; ta.style.left = "-9999px"; ta.style.top = "0";
        document.body.appendChild(ta);
        ta.focus(); ta.select();
        var ok = document.execCommand("copy");
        document.body.removeChild(ta);
        return ok;
      } catch (e) { return false; }
    }
    function flashCopy(btn){
      if (!btn) return;
      var old = btn.textContent;
      btn.classList.add("ok"); btn.textContent = "Copied";
      setTimeout(function(){ btn.classList.remove("ok"); btn.textContent = old; }, 1200);
    }
    function copyDiff(scope, btn){
      if (!scope) return;
      var text = diffText(scope);
      if (navigator.clipboard && navigator.clipboard.writeText){
        navigator.clipboard.writeText(text).then(
          function(){ flashCopy(btn); },
          function(){ if (fallbackCopy(text)) flashCopy(btn); });
      } else if (fallbackCopy(text)) flashCopy(btn);
    }
    function injectCopy(cmp){
      function mk(scope){
        var b = document.createElement("button");
        b.type = "button"; b.className = "copy"; b.setAttribute("data-scope", scope);
        b.appendChild(document.createTextNode("Copy"));
        return b;
      }
      list(cmp, ".file-head").forEach(function(head){
        if (head.querySelector(".copy")) return;
        var b = mk("file");
        b.addEventListener("click", function(e){
          e.stopPropagation(); copyDiff(b.closest(".file"), b);
        });
        head.appendChild(b);
      });
      list(cmp, ".hunk-head").forEach(function(head){
        if (head.querySelector(".copy")) return;
        var b = mk("hunk");
        b.addEventListener("click", function(e){
          e.stopPropagation(); copyDiff(b.closest(".hunk"), b);
        });
        head.appendChild(b);
      });
    }
    function copyFocusedFile(cmp){
      var rail = cmp.querySelector(".filerail");
      var cur = rail ? rail.querySelector(".railchip.cur") : null;
      var id = cur ? cur.getAttribute("data-target") : null;
      var file = id ? document.getElementById(id) : null;
      if (!file) file = curFiles(cmp)[0];
      if (file) copyDiff(file, file.querySelector(".file-head .copy"));
    }

    /* ---- search (DOM Range; never innerHTML) ---- */
    function clearMarks(cmp){
      list(cmp, "mark").forEach(function(m){
        var p = m.parentNode;
        if (!p) return;
        p.replaceChild(document.createTextNode(m.textContent), m);
        p.normalize();
      });
    }
    function markNode(node, q){
      var low = node.nodeValue.toLowerCase(), hits = [], idx = low.indexOf(q);
      while (idx !== -1){ hits.push(idx); idx = low.indexOf(q, idx + q.length); }
      for (var i = hits.length - 1; i >= 0; i--){
        var r = document.createRange();
        r.setStart(node, hits[i]); r.setEnd(node, hits[i] + q.length);
        var mk = document.createElement("mark");
        try { r.surroundContents(mk); } catch (e) {}
      }
    }
    function runSearch(cmp, query){
      clearMarks(cmp);
      var q = (query || "").toLowerCase();
      if (!q) return;
      list(cmp, ".diffview:not([hidden]) .tx").forEach(function(tx){
        var w = document.createTreeWalker(tx, NodeFilter.SHOW_TEXT, null, false);
        var nodes = [], n;
        while ((n = w.nextNode())) nodes.push(n);
        nodes.forEach(function(t){ markNode(t, q); });
      });
    }

    /* ---- help legend ---- */
    function buildLegend(){
      var box = document.createElement("div");
      box.className = "legend"; box.setAttribute("role", "dialog");
      box.setAttribute("aria-label", "diff viewer keyboard shortcuts");
      var h = document.createElement("h4");
      h.appendChild(document.createTextNode("Keyboard")); box.appendChild(h);
      var dl = document.createElement("dl");
      [["j / k", "next / prev hunk"], ["n / p", "next / prev file"],
       ["u", "split / unified"], ["w", "toggle wrap"], ["/", "focus search"],
       ["c", "copy focused file"], ["g / G", "top / bottom"], ["?", "toggle this help"]
      ].forEach(function(p){
        var dt = document.createElement("dt"), kb = document.createElement("kbd");
        kb.appendChild(document.createTextNode(p[0])); dt.appendChild(kb);
        var dd = document.createElement("dd");
        dd.appendChild(document.createTextNode(p[1]));
        dl.appendChild(dt); dl.appendChild(dd);
      });
      box.appendChild(dl);
      document.body.appendChild(box);
      return box;
    }
    function toggleLegend(){
      if (!legendEl) legendEl = buildLegend();
      legendEl.classList.toggle("on");
    }

    /* ---- keyboard navigation (within the active pane) ---- */
    function relTop(pane, el){
      return el.getBoundingClientRect().top - pane.getBoundingClientRect().top;
    }
    function step(list_, pane, dir){
      if (!list_.length) return null;
      var idx = -1;
      for (var i = 0; i < list_.length; i++){
        if (relTop(pane, list_[i]) <= 6) idx = i; else break;
      }
      var ni = idx + dir;
      if (ni < 0) ni = 0;
      if (ni > list_.length - 1) ni = list_.length - 1;
      return list_[ni];
    }
    function activeCmp(){
      var best = cmps[0], score = Infinity, mid = window.innerHeight / 2;
      cmps.forEach(function(c){
        var r = c.getBoundingClientRect();
        if (r.bottom < 0 || r.top > window.innerHeight) return;
        var s = Math.abs((r.top + r.bottom) / 2 - mid);
        if (s < score){ score = s; best = c; }
      });
      return best;
    }
    function onKey(e){
      var t = e.target;
      if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA"
                || t.tagName === "SELECT" || t.isContentEditable)) return;
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      var key = e.key;
      if ("jknpuwcgG/?".indexOf(key) === -1) return;
      var cmp = activeCmp();
      if (!cmp) return;
      var pane = activePane(cmp);
      if (key === "?"){ toggleLegend(); }
      else if (key === "/"){ var s = cmp.querySelector(".diffsearch"); if (s) s.focus(); }
      else if (key === "u"){ toggleSplit(cmp); }
      else if (key === "w"){ toggleWrap(cmp); }
      else if (key === "c"){ copyFocusedFile(cmp); }
      else if (key === "g"){ scrollEnd(pane, false); }
      else if (key === "G"){ scrollEnd(pane, true); }
      else if (key === "j" || key === "k"){
        var el = step(curHunks(cmp), pane, key === "j" ? 1 : -1);
        if (el) scrollPaneTo(pane, el);
      } else if (key === "n" || key === "p"){
        var f = step(curFiles(cmp), pane, key === "n" ? 1 : -1);
        if (f){ scrollPaneTo(pane, f); markCur(cmp, f.id); }
      } else return;
      e.preventDefault();
    }

    /* ---- per-cmp init ---- */
    function initCmp(cmp){
      var task = cmp.getAttribute("data-task");
      var panes = panesOf(cmp);
      if (panes[0]){ panes[0].classList.add("focus"); cmp._focus = panes[0]; }

      list(cmp, "select.runsel").forEach(function(sel){
        sel.addEventListener("change", function(){
          showRun(cmp, sel.getAttribute("data-arm"), sel.value);
        });
      });
      list(cmp, ".modesw .mtab").forEach(function(tab){
        tab.addEventListener("click", function(){ setMode(cmp, tab.getAttribute("data-mode")); });
      });
      list(cmp, ".viewsw .vbtn").forEach(function(b){
        b.addEventListener("click", function(){ setSplit(cmp, b.getAttribute("data-act") === "split"); });
      });
      var wrapBtn = cmp.querySelector(".wrapbtn");
      if (wrapBtn) wrapBtn.addEventListener("click", function(){ toggleWrap(cmp); });
      var search = cmp.querySelector(".diffsearch");
      if (search){
        var deb;
        search.addEventListener("input", function(){
          clearTimeout(deb);
          deb = setTimeout(function(){ runSearch(cmp, search.value); }, 120);
        });
      }
      panes.forEach(function(p){
        p.addEventListener("mousedown", function(){ setFocusPane(cmp, p); });
      });
      injectCopy(cmp);

      // restore persisted prefs (work under file://)
      if (lsGet("skillab:wrap") === "1") setWrap(cmp, true);
      restoreSplit(cmp);
      var savedMode = lsGet("skillab:mode:" + task);
      if (savedMode === "focus" || savedMode === "compare") setMode(cmp, savedMode);

      syncScroll(cmp);
      refresh(cmp);
    }

    cmps.forEach(initCmp);
    document.addEventListener("keydown", onKey);
  }

  mount();
  wireTooltip();
  wireDiff();
  DiffViewer();
})();
"""


def _chart_data(results: list[RunResult], cfg: ExperimentConfig, metrics: list,
                pairs: list, pair_itt: dict, comparisons: list | None,
                rng: random.Random) -> dict:
    """Assemble the JSON the in-page charts render from. `pair_itt` is the
    PRECOMPUTED {pair_key: {metric: DiffEstimate|None}} (no re-bootstrapping).
    `rng` seeds the blind-judge win-rate cluster-bootstrap CI."""
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
        for pkey, agg in aggregate_judge(
                comparisons, ci_iters=cfg.bootstrap_iters,
                alpha=cfg.bootstrap_alpha, rng=rng).items():
            wr = agg["win_rate_a"]
            judge.append({
                "pair": pkey, "a": agg["a_label"], "b": agg["b_label"],
                "a_wins": agg["a_wins"], "b_wins": agg["b_wins"], "ties": agg["ties"],
                "win_rate_a": (wr if wr == wr else None),
                "win_rate_a_ci": agg["win_rate_a_ci"],
                "consistency": (round(100 * agg["consistent"] / agg["total_pairs"])
                                if agg["total_pairs"] else 0)})
    return {"metrics": metrics, "primary": cfg.primary_metric,
            "dir": {m: _metric_direction(cfg, m) for m in metrics},
            "arms": [arm_label(cfg, a) for a in arms], "armColors": arm_colors,
            "tasks": tasks, "taskColors": task_colors, "runs": runs,
            "armMeans": arm_means, "comparisons": comps, "judge": judge,
            "work": _work_blob(results, cfg, arms)}


def _verdict_blob(verdict: dict | None, cross_runner: bool = False) -> dict:
    """Map the primary-pair badge verdict to the hero status pill (text + tone).

    A cross-runner primary pair is confounded (different CLIs / default models /
    logins), so it is downgraded to 'suggestive' -- grey, never a confident green or
    red -- regardless of what the CI shows (plan 022, blocker #4). The measured
    direction is still surfaced as a hint, just not as a verdict."""
    label = verdict["label"] if verdict else "inconclusive"
    if cross_runner:
        hint = {"verified": " (the second CLI scored higher)",
                "regressed": " (the first CLI scored higher)"}.get(label, "")
        return {"label": "suggestive", "tone": "flat", "crossRunner": True,
                "text": "Suggestive — comparing agent CLIs, not a controlled "
                        "skill/model change" + hint}
    text = {"verified": "Significant effect", "regressed": "Regression detected",
            "inconclusive": "Inconclusive at this scale"}[label]
    tone = {"verified": "good", "regressed": "bad", "inconclusive": "flat"}[label]
    return {"label": label, "tone": tone, "crossRunner": False, "text": text}


def _run_patch_id(tid: str, arm_value: str, run_index: int) -> str:
    """Stable, DOM-safe id for one run's rendered diff (task+arm+run). Shared by the
    server render and the `work` blob so their `f-<pid>-<fi>` file ids line up. The
    charset (`[0-9A-Za-z-]`) is safe to interpolate raw into an id/attribute/value."""
    return re.sub(r"[^0-9A-Za-z]+", "-", f"{tid}-{arm_value}-{run_index}")


def _shown_runs(results: list[RunResult], tid: str, arm,
                limit: int = 3) -> list[RunResult]:
    """The valid runs surfaced for one (task, arm) in the diff viewer, capped so a
    high-k experiment doesn't render dozens of near-identical diffs."""
    return [r for r in results
            if r.task_id == tid and r.arm is arm and r.itt_valid][:limit]


def _work_blob(results: list[RunResult], cfg: ExperimentConfig, arms: list) -> dict:
    """Per-task wiring data for the DiffViewer (plan 024 §1.3). Carries ONLY numeric
    counts, DOM ids, status letters and truncation flags -- NEVER diff text, paths or
    code. The diff DOM is pre-rendered + escaped server-side; this blob just lets the
    JS pick runs and label the file rail without re-parsing content. (A test asserts no
    diff substring reaches `window.SKILL_AB`.)"""
    work: dict = {}
    for tid in sorted({r.task_id for r in results}):
        files: list[dict] = []
        arms_blob: dict = {}
        truncated: dict = {}
        for arm in arms:
            run_ids: list[str] = []
            for r in _shown_runs(results, tid, arm):
                pid = _run_patch_id(tid, arm.value, r.run_index)
                run_ids.append(pid)
                truncated[pid] = bool(r.diff_truncated)
                # Re-parse to mirror the rendered file ids/counts -- numbers + a single
                # status letter only; the path stays in the (escaped) DOM, not here.
                for fi, pf in enumerate(parse_unified_diff(r.diff)):
                    files.append({"id": f"f-{pid}-{fi}", "add": pf.add_count,
                                  "del": pf.del_count, "status": pf.status})
            arms_blob[arm.value] = {"run_ids": run_ids,
                                    "rep": run_ids[0] if run_ids else None}
        work[tid] = {"files": files, "arms": arms_blob, "truncated": truncated}
    return work


def _work_products_html(results: list[RunResult], cfg: ExperimentConfig,
                        arms: list) -> str:
    """Per-task interactive diff comparison -- the `.cmp` shell (plan 024 §1.2). Each
    task gets a sticky toolbar (per-arm run pickers, Compare/Focus tabs, Unified/Split,
    Wrap, a search box, a JS-filled file rail) over one `.pane` per arm holding that
    arm's escaped `render_diff` output. The DiffViewer IIFE wires the behavior; every
    diff byte is escaped server-side and never reaches the JSON blob nor an
    innerHTML-of-raw path (the diffs are the only user-controlled content here)."""
    parts = ['<div class="section-h"><h2>Work products</h2>'
             '<span class="hint">actual diffs each arm produced &mdash; arm vs arm'
             '</span></div>']
    for tid in sorted({r.task_id for r in results}):
        et = html.escape(tid)
        counts = " / ".join(
            f"{len(_shown_runs(results, tid, arm))} {html.escape(arm_label(cfg, arm))}"
            for arm in arms)
        parts.append(f'<div class="cmp" data-task="{et}">')
        # ---- toolbar -------------------------------------------------------
        parts.append('<div class="cmp-bar">')
        parts.append(f'<div class="cmp-task">{et}<span class="count">{counts}'
                     '</span></div>')
        parts.append('<div class="armrun">')
        for arm in arms:
            runs = _shown_runs(results, tid, arm)
            opts = "".join(
                f'<option value="{_run_patch_id(tid, arm.value, r.run_index)}">'
                f"run {r.run_index}</option>" for r in runs)
            dis = "" if runs else " disabled"
            parts.append(
                f'<label class="armpick"><span class="al">'
                f"{html.escape(arm_label(cfg, arm))}</span>"
                f'<select class="runsel" data-arm="{arm.value}"{dis}>{opts}'
                "</select></label>")
        parts.append("</div>")
        parts.append(
            '<div class="modesw" role="tablist" aria-label="comparison mode">'
            '<button class="mtab" role="tab" data-mode="compare" '
            'aria-selected="true">Compare</button>'
            '<button class="mtab" role="tab" data-mode="focus" '
            'aria-selected="false">Focus</button></div>')
        parts.append(
            '<div class="viewsw" role="group" aria-label="diff layout">'
            '<button class="vbtn" data-act="unified" aria-pressed="true">Unified'
            '</button><button class="vbtn" data-act="split" aria-pressed="false">'
            "Split</button></div>")
        parts.append('<button class="vbtn wrapbtn" data-act="wrap" '
                     'aria-pressed="false">Wrap</button>')
        parts.append(f'<input class="diffsearch" type="search" '
                     f'placeholder="search diffs (/)" '
                     f'aria-label="search diffs in {et}">')
        parts.append('<div class="filerail" aria-label="files in this task"></div>')
        parts.append("</div>")  # .cmp-bar
        # ---- panes ---------------------------------------------------------
        parts.append('<div class="panes" data-mode="compare">')
        for arm in arms:
            runs = _shown_runs(results, tid, arm)
            parts.append(
                f'<div class="pane" data-arm="{arm.value}" data-task="{et}">'
                f'<div class="pane-h">{html.escape(arm_label(cfg, arm))}</div>')
            if not runs:
                parts.append('<p class="empty">(no valid runs)</p>')
            for j, r in enumerate(runs):
                pid = _run_patch_id(tid, arm.value, r.run_index)
                hide = "" if j == 0 else " hidden"
                parts.append(
                    f'<div class="diffview" data-run="{pid}"{hide}>'
                    f'<div class="wp-meta">run {r.run_index} {_MIDDOT} '
                    f"${r.cost_usd or 0:.3f} {_MIDDOT} {r.num_turns or '?'} turns "
                    f"{_MIDDOT} act={r.skill_activated}</div>"
                    f"{render_diff(r.diff, pid, r.diff_truncated)}</div>")
            parts.append("</div>")  # .pane
        parts.append("</div>")  # .panes
        parts.append("</div>")  # .cmp
    return "".join(parts)


def _judge_reasons_html(comparisons: list) -> str:
    """Per-pair blind-judge verdicts + reasons. Escaped server-side."""
    parts = ["<div class='section-h'><h2>Blind judge " + _MIDDOT
             + " reasons</h2><span class='hint'>NON-DETERMINISTIC</span></div>"]
    for pkey in dict.fromkeys(c.pair for c in comparisons):
        rows = [c for c in comparisons if c.pair == pkey]
        la, lb = rows[0].a_label, rows[0].b_label
        parts.append(
            f"<details class='det'><summary><span class='caret'>{_CARET}</span>"
            f"{html.escape(la)} vs {html.escape(lb)}"
            f"<span class='count'>{len(rows)} verdicts</span></summary>"
            "<div class='det-body'><table class='tbl'><thead><tr>"
            "<th class='l'>Task</th><th>Pair</th><th>Ordering</th>"
            "<th class='l'>Winner</th><th class='l'>Reason</th></tr></thead><tbody>")
        for c in rows:
            parts.append(
                f"<tr><td class='l'>{html.escape(c.task_id)}</td>"
                f"<td>{c.pair_id}</td><td>{html.escape(c.ordering)}</td>"
                f"<td class='l mono'>{html.escape(str(c.winner_arm))}</td>"
                f"<td class='l wrap'>{html.escape(c.reason or '')}</td></tr>")
        parts.append("</tbody></table></div></details>")
    return "".join(parts)


def build_html_report(results: list[RunResult], pf: Preflight, cfg: ExperimentConfig,
                      manifest: dict, comparisons: list | None = None,
                      seed: int = 0) -> str:
    """One self-contained, offline .html dashboard: header + verdict hero + arm
    cards + interactive metric charts + blind-judge panel + collapsible audit
    (stats, all-runs, work-product diffs, judge reasons). No external assets; every
    figure is hand-built inline SVG driven by the embedded `window.SKILL_AB` blob."""
    scorers = default_scorers(cfg)
    metrics = [s.name for s in scorers] + ["cost_usd"]
    arms = experiment_arms(cfg)
    pairs = experiment_pairs(cfg)
    total_contam = sum(1 for r in results if r.contaminated)

    # One shared estimator pass: identical CIs to summary.json / badge / markdown.
    est = compute_estimates(results, cfg, metrics, seed)
    pair_itt = {key: p["itt"] for key, p in est.items()}

    pa, pb = primary_pair(cfg)
    pe = pair_itt[pair_key(cfg, pa, pb)][cfg.primary_metric]
    verdict = (badge_verdict({"ci_low": pe.ci_low, "ci_high": pe.ci_high, "point": pe.point},
                             _metric_direction(cfg, cfg.primary_metric), pe.n_tasks,
                             pe.clustered, total_contam) if pe else None)
    title = cfg.skill_name if len(pairs) == 1 else f"{cfg.skill_name} vs {cfg.skill_b_name}"

    # Fresh seeded rng for the judge win-rate CI (independent of compute_estimates'
    # own rng; deterministic for a given seed).
    data = _chart_data(results, cfg, metrics, pairs, pair_itt, comparisons,
                       random.Random(seed))
    cross_runner = _pair_is_cross_runner(cfg, pa, pb)
    data["verdict"] = _verdict_blob(verdict, cross_runner)
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
        "crossRunner": cross_runner,
        "armRunners": {arm_label(cfg, a): _runner_label(arm_runner(cfg, a))
                       for a in arms},
    }
    blob = json.dumps(data).replace("</", "<\\/")

    server_detail = _work_products_html(results, cfg, arms)
    if comparisons:
        server_detail += _judge_reasons_html(comparisons)

    footer = (
        f"skill A/B harness {html.escape(str(manifest['harness_version']))} {_MIDDOT} "
        f"repo {html.escape(manifest['repo_path'])} @ {html.escape(manifest['base_ref'])} "
        f"({(manifest.get('base_ref_sha') or '?')[:12]}) {_MIDDOT} "
        f"SKILL.md {(manifest.get('skill_md_sha256') or '?')[:16]} {_MIDDOT} "
        f"intention-to-treat estimand {_MIDDOT} activation is a diagnostic, not a gate "
        f"{_MIDDOT} rendered offline, no network calls")

    parts = [
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>",
        "<meta name='viewport' content='width=device-width, initial-scale=1'>",
        f"<title>skill A/B — {html.escape(title)}</title>",
        f"<style>{_HTML_STYLE}</style></head><body>",
        "<div class='wrap'><div id='app'></div>",
        server_detail,
        f"<footer>{footer}</footer>",
        "</div><div id='tip' role='tooltip' aria-hidden='true'></div>",
        f"<script>window.SKILL_AB={blob};\n{_HTML_SCRIPT}</script>",
        "</body></html>",
    ]
    return "".join(parts)


# ---------------------------------------------------------------------------
# Cost / time projection + minimum-detectable-effect (planning, no spend)
# ---------------------------------------------------------------------------

def _should_stop(spent: float, ceiling: float | None) -> bool:
    return ceiling is not None and spent >= ceiling


def estimate_cost(cfg: ExperimentConfig, tasks: list[Task],
                  per_run_usd: float | None = None,
                  per_run_seconds: float | None = None) -> dict:
    """Project total spend/time. n_runs scales with the number of arms (3 for a
    head-to-head). Judge calls when judge_enabled: 2 orderings × min(k,
    judge_max_pairs_per_task) × tasks × number-of-comparison-pairs."""
    n_arms = len(experiment_arms(cfg))
    n_pairs = len(experiment_pairs(cfg))
    n_runs = len(tasks) * n_arms * cfg.k
    n_judge = (2 * min(cfg.k, cfg.judge_max_pairs_per_task) * len(tasks) * n_pairs
               if cfg.judge_enabled else 0)
    cpr = per_run_usd if per_run_usd is not None else cfg.cost_per_run_usd
    total_cost = None if cpr is None else cpr * (n_runs + n_judge)
    wall = None
    if per_run_seconds is not None:
        import math
        waves = math.ceil((n_runs + n_judge) / max(1, cfg.max_concurrency))
        wall = per_run_seconds * waves
    return {"n_runs": n_runs, "n_judge_calls": n_judge, "per_run_usd": cpr,
            "projected_usd": total_cost, "projected_wall_seconds": wall,
            "ceiling_usd": cfg.cost_ceiling_usd}


def _smallest_delta_reaching_power(deltas: list[float], power_at,
                                   power: float) -> float | None:
    """Left-bisection over an ASCENDING `deltas` grid: the smallest deltas[i]
    whose power_at(i) >= `power`, or None if none reach it. Correct only because
    simulated power is monotone non-decreasing in the true delta, so the
    predicate `power_at(i) >= power` is a single False->True step; bisection
    finds its boundary in ~log2(len) probes instead of scanning the whole grid.
    `power_at` maps a grid index to an estimated power in [0, 1]."""
    lo, hi = 0, len(deltas)
    while lo < hi:
        mid = (lo + hi) // 2
        if power_at(mid) >= power:
            hi = mid
        else:
            lo = mid + 1
    return deltas[lo] if lo < len(deltas) else None


def minimum_detectable_effect(cfg: ExperimentConfig, n_tasks: int, baseline: float,
                              noise_sd: float, power: float = 0.8, seed: int = 0,
                              deltas: list[float] | None = None,
                              sims: int = 200) -> float | None:
    """Smallest effect detectable at `power` for the current k and n_tasks, given a
    per-run noise SD. Reuses cluster_bootstrap_ci — no numpy. Simulated power is
    monotone non-decreasing in the true delta, so we BINARY-SEARCH the grid
    (~log2 N probes) instead of scanning all deltas. Each delta is evaluated with
    its OWN rng, seeded from (seed, grid index), so the estimate is independent of
    the order deltas are probed in — a full linear scan and the bisection agree."""
    deltas = deltas or [i / 100 for i in range(1, 51)]   # 0.01 .. 0.50
    tasks = [f"t{i}" for i in range(n_tasks)]

    def power_at(idx: int) -> float:
        # Per-delta seed: a distinct rng stream per grid index (prime stride > any
        # realistic index) so probing deltas out of order during bisection yields
        # the same estimate a full in-order scan would.
        rng = random.Random(seed * 1_000_003 + idx)
        center_on = baseline + deltas[idx]
        hits = 0
        for _ in range(sims):
            on = {t: [center_on + rng.gauss(0, noise_sd) for _ in range(cfg.k)]
                  for t in tasks}
            off = {t: [baseline + rng.gauss(0, noise_sd) for _ in range(cfg.k)]
                   for t in tasks}
            lo, hi, _ = cluster_bootstrap_ci(on, off, tasks, 400, cfg.bootstrap_alpha, rng)
            if not (lo <= 0 <= hi):
                hits += 1
        return hits / sims

    return _smallest_delta_reaching_power(deltas, power_at, power)


def within_arm_noise(results: list[RunResult], metric: str, arm: Arm) -> float:
    """Std dev of a metric within one arm's valid runs — a quick noise indicator
    (used by the methodology / power guidance)."""
    vals = [r.scores[metric] for r in results
            if r.arm is arm and r.itt_valid and metric in r.scores]
    return statistics.pstdev(vals) if len(vals) > 1 else 0.0


# ---------------------------------------------------------------------------
# Config loading + CI policy
# ---------------------------------------------------------------------------

def load_config(config_path: Path) -> tuple[ExperimentConfig, list[Task]]:
    """Build the experiment from a TOML file. [experiment] maps to ExperimentConfig
    (Path-typed fields accept strings); [[task]] tables map to Task. Unknown keys
    raise, so typos fail loud."""
    data = tomllib.loads(config_path.read_text())
    exp = dict(data.get("experiment", {}))
    for k in ("repo_path", "skill_src", "skill_b_src", "worktree_root", "results_dir"):
        if k in exp:
            exp[k] = Path(exp[k]).expanduser()
    if "allowed_tools" in exp:
        exp["allowed_tools"] = tuple(exp["allowed_tools"])
    cfg = ExperimentConfig(**exp)
    tasks = [Task(**t) for t in data.get("task", [])]
    if not tasks:
        raise ValueError(f"{config_path}: no [[task]] tables found")
    return cfg, tasks


def ci_exit_code(summary: dict, cfg: ExperimentConfig, policy: str) -> tuple[int, str]:
    """CI gate. policy:
       'no-regression' — fail only on a significant regression of the primary metric.
       'require-improvement' — fail unless a significant improvement is shown.
    Returns (exit_code, human message)."""
    v = primary_verdict(summary, cfg)
    metric = summary["primary_metric"]
    if v is None:
        return (1, f"no ITT estimate for primary metric '{metric}'")
    if policy == "require-improvement":
        if v["label"] == "verified":
            return (0, f"PASS: {metric} verified improvement {v['point'] * 100:+.0f}%")
        return (1, f"FAIL (require-improvement): {metric} is {v['label']}")
    # default: no-regression
    if v["label"] == "regressed":
        return (1, f"FAIL (no-regression): {metric} regressed {v['point'] * 100:+.0f}%")
    return (0, f"PASS (no-regression): {metric} is {v['label']}")


# ---------------------------------------------------------------------------
# Command-line interface
# ---------------------------------------------------------------------------

_INIT_TEMPLATE = '''# skill-ab experiment — edit then run `skill-ab run -c {name}`
[experiment]
repo_path  = "{repo}"      # a git repo (worktrees fork from base_ref)
base_ref   = "main"
skill_src  = "{skill_src}" # the (primary) SKILL.md folder under test
skill_name = "{skill_name}"
# Head-to-head: uncomment to compare TWO skills. The experiment then runs 3 arms
# (skill A, skill B, no-skill control) and reports every pairwise comparison.
# skill_b_src  = "./.claude/skills/other-skill"
# skill_b_name = "other-skill"
k          = 5             # runs per arm per task (k=1 proves nothing)
# judge_enabled = true     # opt-in qualitative axis: the head-to-head "which code is better"
# cost_ceiling_usd = 20.0  # abort once total spend crosses this

[[task]]
id        = "example-task"
prompt    = "Describe a real change you'd ask the agent to make, with tests."
setup_cmd = "npm ci --silent"   # REQUIRED for repos with deps; a fresh worktree has none
test_cmd  = "npm test --silent"
lint_cmd  = "npm run lint --silent"
build_cmd = "npm run build --silent"
'''


def cmd_init(out_path: Path) -> None:
    """Write a starter skillab.toml, pre-filled from the cwd."""
    cwd = Path.cwd()
    skills = sorted(cwd.glob(".claude/skills/*/SKILL.md"))
    skill_dir = skills[0].parent if skills else Path("./.claude/skills/my-skill")
    if out_path.exists():
        raise SystemExit(f"{out_path} already exists; refusing to overwrite")
    out_path.write_text(_INIT_TEMPLATE.format(
        name=out_path.name, repo=str(cwd), skill_src=str(skill_dir), skill_name=skill_dir.name))
    print(f"wrote {out_path} — edit it, then: skill-ab run -c {out_path}")


def _build_example() -> tuple[ExperimentConfig, list[Task]]:
    cfg = ExperimentConfig(
        repo_path=Path("/path/to/your/repo"), base_ref="main",
        skill_src=Path("/path/to/skills/my-skill"), skill_name="my-skill", k=5)
    tasks = [Task(id="add-pagination",
                  prompt="Add cursor-based pagination to /items, with tests.",
                  setup_cmd="npm ci --silent", test_cmd="npm test --silent",
                  lint_cmd="npm run lint --silent", build_cmd="npm run build --silent")]
    return cfg, tasks


def _clone_from_github(url: str, config_name: str) -> tuple[ExperimentConfig, list[Task]]:
    """Shallow-clone a skill repo and read its committed skill-ab config. The repo's
    comparison is then reproducible from just the URL.

    SECURITY: the cloned config's setup_cmd/test_cmd/prompt run on THIS machine and
    drive `claude -p` over the cloned code. Only the caller (gated by --trust-remote)
    should reach here. `--` stops a hostile `url` from being read as a git option."""
    dest = Path(tempfile.mkdtemp(prefix="skill_ab_clone_"))
    subprocess.run(["git", "clone", "--depth", "1", "--", url, str(dest)], check=True)
    cfg_path = dest / config_name
    if not cfg_path.exists():
        raise SystemExit(f"{url} has no {config_name} at its root")
    cfg, tasks = load_config(cfg_path)
    # Point the experiment at the clone regardless of what the toml said.
    cfg = dataclasses_replace(cfg, repo_path=dest)
    return cfg, tasks


def dataclasses_replace(cfg: ExperimentConfig, **changes) -> ExperimentConfig:
    import dataclasses
    return dataclasses.replace(cfg, **changes)


def _run_and_outputs(cfg: ExperimentConfig, tasks: list[Task], resume: bool = False,
                     html_out: Path | None = None):
    results, pf = asyncio.run(run_experiment(cfg, tasks, resume=resume))
    manifest = experiment_manifest(cfg, seed=0, timestamp=time.time())
    print(build_report(results, pf, cfg, manifest=manifest))
    comparisons = None
    if cfg.judge_enabled:
        comparisons = asyncio.run(run_qualitative_judge(results, tasks, cfg))
        print("\n" + build_judge_report(comparisons, cfg, results))
        # Persist so `report` can rebuild the judge charts offline (no re-judging).
        (cfg.results_dir / "judge.jsonl").write_text(
            "".join(json.dumps(c.__dict__) + "\n" for c in comparisons))
    summary = summary_dict(results, cfg, manifest)
    cfg.results_dir.mkdir(parents=True, exist_ok=True)
    (cfg.results_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    if html_out:
        html_out.write_text(build_html_report(results, pf, cfg, manifest, comparisons))
        print(f"\nHTML report: {html_out}")
    return results, pf, summary, manifest


def _demo_results() -> list[RunResult]:
    """The bundled example, generated IN-CODE so it ships with the single module
    (no data file needed) and `uvx skill-ab demo` works offline. Honest story: the
    skill clearly helps tests_pass on 2 tasks, no contamination -> a real 'verified'."""
    data = {
        "add-email-validation": {
            Arm.SKILL_ON: [(1, 1, 42, True), (1, 1, 38, True), (1, 1, 55, True),
                           (1, 1, 40, True), (1, 1, 47, False), (0, 1, 12, True)],
            Arm.SKILL_OFF: [(0, 1, 9, False), (0, 0, 7, False), (0, 1, 14, False),
                            (1, 1, 22, False), (0, 1, 5, False), (0, 1, 11, False)],
        },
        "fix-null-deref": {
            Arm.SKILL_ON: [(1, 1, 18, True), (1, 1, 21, True), (1, 1, 16, True),
                           (1, 1, 24, True), (1, 1, 19, True), (1, 1, 20, True)],
            Arm.SKILL_OFF: [(1, 1, 8, False), (0, 1, 6, False), (1, 0, 10, False),
                            (0, 1, 5, False), (0, 1, 7, False), (1, 1, 9, False)],
        },
    }
    diff_on = (
        "diff --git a/util.py b/util.py\n"
        "--- a/util.py\n+++ b/util.py\n"
        "@@ -1,5 +1,7 @@\n"
        " import re\n"
        " \n"
        "-def is_valid_email(s):\n"
        "-    return '@' in s\n"
        "+def is_valid_email(s: str) -> bool:\n"
        "+    pattern = r'^[^@]+@[^@]+\\.[^@]+$'\n"
        "+    return bool(re.match(pattern, s))\n"
        " \n"
        " EMAIL_MAX = 254\n"
        "diff --git a/test_util.py b/test_util.py\n"
        "new file mode 100644\n"
        "--- /dev/null\n+++ b/test_util.py\n"
        "@@ -0,0 +1,4 @@\n"
        "+def test_valid():\n"
        "+    assert is_valid_email('a@b.com')\n"
        "+    assert not is_valid_email('nope')\n"
        "+    assert not is_valid_email('')\n"
    )
    diff_off = (
        "diff --git a/util.py b/util.py\n"
        "--- a/util.py\n+++ b/util.py\n"
        "@@ -1,3 +1,3 @@\n"
        " import re\n"
        " \n"
        "-def is_valid_email(s):\n"
        "+def is_valid_email(s):  # quick check\n"
        "     return '@' in s\n"
    )
    out = []
    for tid, arms in data.items():
        for arm, runs in arms.items():
            for i, (tp, lp, dl, act) in enumerate(runs):
                r = RunResult(
                    task_id=tid, arm=arm, run_index=i,
                    worktree=Path(f"/tmp/wt/{tid}-{arm.value}-{i}"),
                    skill_activated=(act if arm is Arm.SKILL_ON else None),
                    arm_skill_name=("write-tests-first" if arm is Arm.SKILL_ON else None),
                    activation_reason=("Skill tool invoked 'write-tests-first'"
                                       if act and arm is Arm.SKILL_ON
                                       else "no activation signal observed"),
                    agent_ok=True, completed=True, cost_usd=round(0.05 + 0.01 * i, 3),
                    num_turns=4 + i, wall_seconds=30.0 + i,
                    diff=(diff_on if arm is Arm.SKILL_ON else diff_off))
                r.scores = {"tests_pass": float(tp), "lint_pass": float(lp),
                            "diff_lines": float(dl), "cost_usd": r.cost_usd}
                out.append(r)
    return out


def cmd_demo(out_dir: Path) -> None:
    """Render the in-code bundled example into an HTML report + badge with ZERO
    claude/git/network/cost. Always offline -- this command never spends money."""
    out_dir.mkdir(parents=True, exist_ok=True)
    results = _demo_results()
    cfg = ExperimentConfig(repo_path=Path("."), base_ref="HEAD",
                           skill_src=Path("demo_skill"),
                           skill_name="write-tests-first", results_dir=out_dir, k=6)
    manifest = experiment_manifest(cfg, seed=0, timestamp=0.0, offline=True)
    html_path = out_dir / "report.html"
    html_path.write_text(build_html_report(results, Preflight(), cfg, manifest))
    summary = summary_dict(results, cfg, manifest)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    verdict = primary_verdict(summary, cfg)
    svg_path = out_dir / "badge.svg"
    if verdict:
        svg_path.write_text(render_badge_svg(cfg.primary_metric, verdict))
    print(f"demo report: {html_path}\ndemo badge:  {svg_path}\n"
          f"open the HTML to see the on/off diff drill-down. No money spent.")


_PR_RE = re.compile(r"github\.com/([^/]+/[^/]+?)(?:\.git)?/pull/(\d+)")


def _detect_commands(repo: Path) -> tuple[str | None, str | None]:
    """Best-effort (setup_cmd, test_cmd) for a checked-out repo, by stack."""
    if (repo / "package.json").exists():
        if (repo / "pnpm-lock.yaml").exists():
            return "pnpm install --frozen-lockfile", "pnpm test"
        if (repo / "yarn.lock").exists():
            return "yarn install --frozen-lockfile", "yarn test"
        return "npm ci", "npm test --silent"
    if (repo / "pyproject.toml").exists():
        return "pip install -e .", "pytest -q"
    if (repo / "requirements.txt").exists():
        return "pip install -r requirements.txt", "pytest -q"
    if (repo / "go.mod").exists():
        return "go mod download", "go test ./..."
    return None, None


def _norm_remote(url: str) -> str:
    return re.sub(r"^(git@github\.com:|https?://)", "", url).replace(".git", "").rstrip("/").lower()


def _ensure_pr_repo(owner_repo: str, _url: str) -> Path:
    """Use the current repo if its origin matches; else shallow-clone via gh."""
    cur = subprocess.run(["git", "remote", "get-url", "origin"],
                         capture_output=True, text=True)
    if cur.returncode == 0:
        # EXACT owner/repo match (not endswith — 'sub-acme/repo' must not match 'acme/repo').
        if _norm_remote(cur.stdout.strip()).split("/")[-2:] == owner_repo.lower().split("/"):
            return Path.cwd()
    dest = Path(tempfile.mkdtemp(prefix="skill_ab_repo_")) / owner_repo.split("/")[-1]
    subprocess.run(["gh", "repo", "clone", owner_repo, str(dest), "--", "--depth", "50"],
                   check=True)
    return dest


def _fetch_pr_comments(url: str, owner_repo: str, num: str, repo: Path) -> str:
    """READ-ONLY: fetch the PR's review + inline comments to inject into the task, so
    the agent never needs `gh` to read them (and gh writes can be safely denied)."""
    lines: list[str] = []
    try:
        out = subprocess.run(["gh", "pr", "view", url, "--json", "reviews,comments"],
                             cwd=repo, capture_output=True, text=True, timeout=60)
        data = json.loads(out.stdout or "{}")
        for r in data.get("reviews", []):
            if r.get("body"):
                who = (r.get("author") or {}).get("login", "?")
                lines.append(f"- [review by {who}] {r['body'].strip()}")
        for c in data.get("comments", []):
            if c.get("body"):
                who = (c.get("author") or {}).get("login", "?")
                lines.append(f"- [comment by {who}] {c['body'].strip()}")
    except Exception:  # noqa: BLE001
        pass
    try:                                   # inline review-thread comments (need the API, GET)
        api = subprocess.run(
            ["gh", "api", f"repos/{owner_repo}/pulls/{num}/comments", "--paginate"],
            cwd=repo, capture_output=True, text=True, timeout=60)
        for c in json.loads(api.stdout or "[]"):
            if c.get("body"):
                lines.append(f"- [{c.get('path', '?')}:{c.get('line', '?')}] {c['body'].strip()}")
    except Exception:  # noqa: BLE001
        pass
    return "\n".join(lines) or "(no open review comments found via gh)"


def resolve_target(target: str, prompt: str | None = None
                   ) -> tuple[Path, str, str, str | None, str | None]:
    """Resolve a target (GitHub PR URL / branch / '.'/path) to
    (repo_path, base_ref, task_prompt, setup_cmd, test_cmd)."""
    m = _PR_RE.search(target)
    if m:
        owner_repo, num = m.group(1), m.group(2)   # num is \d+; validate owner/repo
        owner, _, reponame = owner_repo.partition("/")
        if (not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", owner_repo)
                or ".." in owner_repo or owner.startswith("-") or reponame.startswith("-")):
            raise SystemExit(f"unsafe owner/repo in URL: {owner_repo!r}")
        repo = _ensure_pr_repo(owner_repo, target)
        # Force-fetch the PR head into a private local ref WITHOUT touching the working
        # tree or current branch (worktrees fork from this ref). The `+` makes a
        # re-run on a force-pushed PR succeed. The default prompt carries the PR URL so
        # the skill can find it without branch auto-discovery.
        local_ref = f"skill-ab/pr-{num}"
        subprocess.run(["git", "fetch", "origin", f"+pull/{num}/head:{local_ref}"],
                       cwd=repo, check=True)
        setup, test = _detect_commands(repo)
        comments = _fetch_pr_comments(target, owner_repo, num, repo)
        default = (
            f"Address the open review comments on pull request {target} by editing the "
            f"code in this repository. IMPORTANT: do NOT use `gh`, do NOT reply to or "
            f"resolve any comment/thread on GitHub, and do NOT push — make LOCAL code "
            f"changes ONLY (the changes are scored automatically).\n\n"
            f"Open review comments:\n{comments}")
        return repo, local_ref, prompt or default, setup, test
    p = Path(target).expanduser()
    if target in (".", "") or p.is_dir():
        repo = p.resolve() if p.is_dir() else Path.cwd()
        if not prompt:
            raise SystemExit("a directory target needs --prompt (what should the agent do?).")
        return (repo, "HEAD", prompt, *_detect_commands(repo))
    # otherwise: a branch/ref in the current repo
    if target.startswith("-"):                     # never let a ref be read as a git flag
        raise SystemExit(f"invalid branch/ref {target!r} (leading dash).")
    if not prompt:
        raise SystemExit("a branch target needs --prompt (what should the agent do?).")
    return (Path.cwd(), target, prompt, *_detect_commands(Path.cwd()))


def _build_quick(args) -> tuple[ExperimentConfig, list[Task]]:
    if _PR_RE.search(args.target) and not getattr(args, "trust_remote", False):
        raise SystemExit(
            "refusing a PR target without --trust-remote: the harness FETCHES and "
            "EXECUTES the PR's code (setup_cmd/test_cmd) and runs `claude` on it. A PR "
            "(especially from a fork) is untrusted. Review it, then re-run with --trust-remote.")
    md_a = resolve_skill(args.skill_a)
    skill_b_dir = name_b = None
    if args.skill_b.lower() not in ("none", "off", "-", "control", ""):
        md_b = resolve_skill(args.skill_b)
        skill_b_dir, name_b = md_b.parent, args.skill_b
    repo, base_ref, prompt, setup, test = resolve_target(args.target, args.prompt)
    # For a PR target, FORBID GitHub mutation so no run can reply to / resolve review
    # threads or push -- otherwise the first run would corrupt the shared PR state for
    # every other run (and spam the real PR). Comments are pre-injected into the prompt.
    deny = ("Bash(gh:*)", "Bash(git push:*)") if _PR_RE.search(args.target) else ()
    cfg = ExperimentConfig(
        repo_path=repo, base_ref=base_ref,
        skill_src=md_a.parent, skill_name=args.skill_a,
        skill_b_src=skill_b_dir, skill_b_name=name_b,
        k=args.k, judge_enabled=not args.no_judge, disallowed_tools=deny,
        isolation="worktree" if args.worktree_isolation else "inject")
    if getattr(args, "no_tests", False):
        setup = test = None                # judge + diff only (no deps install / suite)
    task = Task(id="task", prompt=prompt,
                setup_cmd=args.setup_cmd or setup, test_cmd=args.test_cmd or test)
    return cfg, [task]


def quick_main(argv: list[str] | None = None) -> int:
    """`skill-test <skillA> <skillB|none> <target>` — the one-line head-to-head."""
    import argparse
    p = argparse.ArgumentParser(
        prog="skill-test",
        description="Quick head-to-head: skill-test <skillA> <skillB|none> <PR-url|branch|.>")
    p.add_argument("skill_a", help="first skill name (or path to its folder)")
    p.add_argument("skill_b", help="second skill name, or none/off/- for skill-A-vs-control")
    p.add_argument("target", help="a GitHub PR URL, a branch name, or '.'/a path")
    p.add_argument("--prompt", default=None, help="task prompt (defaulted for PR targets)")
    p.add_argument("--test-cmd", default=None)
    p.add_argument("--setup-cmd", default=None)
    p.add_argument("--no-tests", action="store_true",
                   help="skip deps install + the test suite (judge + diff only) — best for "
                        "PR-resolution head-to-heads where the full suite is too heavy")
    p.add_argument("-k", type=int, default=3, help="runs per arm per task (default 3)")
    p.add_argument("--no-judge", action="store_true", help="skip the qualitative judge")
    p.add_argument("--html", type=Path, default=Path("skill-ab-report.html"))
    p.add_argument("--worktree-isolation", action="store_true",
                   help="install skills into the worktree instead of force-injecting "
                        "(only valid if the skills are NOT globally installed)")
    p.add_argument("--trust-remote", action="store_true",
                   help="REQUIRED for a PR target: you trust the PR's code/test_cmd to "
                        "run on this machine")
    args = p.parse_args(argv)
    cfg, tasks = _build_quick(args)
    _run_and_outputs(cfg, tasks, html_out=args.html)
    return 0


def _load_cfg_tasks(args) -> tuple[ExperimentConfig, list[Task]]:
    if getattr(args, "example", False):
        return _build_example()
    if getattr(args, "from_github", None):
        if not getattr(args, "trust_remote", False):
            raise SystemExit(
                "refusing --from-github without --trust-remote: a remote skillab.toml's "
                "setup_cmd/test_cmd run shell on YOUR machine and drive claude over the "
                "cloned code. Re-run with --trust-remote only if you trust that repo.")
        return _clone_from_github(args.from_github, args.config.name)
    return load_config(args.config)


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else argv
    if raw and raw[0] == "quick":          # `skill-ab quick A B TARGET` == `skill-test`
        return quick_main(raw[1:])
    import argparse
    p = argparse.ArgumentParser(prog="skill-ab",
                                description="A/B test whether a Claude Code skill helps.")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    pi = sub.add_parser("init", help="scaffold a skillab.toml in the current repo")
    pi.add_argument("-c", "--config", type=Path, default=Path("skillab.toml"))

    def _cfg_args(sp):
        sp.add_argument("-c", "--config", type=Path, default=Path("skillab.toml"))
        sp.add_argument("--example", action="store_true", help="use the built-in example config")
        sp.add_argument("--from-github", default=None,
                        help="clone a skill repo URL and run its committed config")
        sp.add_argument("--trust-remote", action="store_true",
                        help="REQUIRED with --from-github: you trust the remote repo's "
                             "setup_cmd/test_cmd to run shell on this machine")

    pr = sub.add_parser("run", help="run the A/B experiment from a config")
    _cfg_args(pr)
    pr.add_argument("--resume", action="store_true", help="reuse the JSONL you already paid for")
    pr.add_argument("--html", type=Path, default=None,
                    help="also write a self-contained HTML report")

    prep = sub.add_parser("report", help="render an HTML report from a results.jsonl (no spend)")
    prep.add_argument("-c", "--config", type=Path, default=Path("skillab.toml"))
    prep.add_argument("--jsonl", type=Path, default=None)
    prep.add_argument("-o", "--out", type=Path, default=Path("skill-ab-report.html"))

    pb = sub.add_parser("badge", help="emit an SVG + markdown badge from summary.json")
    pb.add_argument("-s", "--summary", type=Path, default=Path("summary.json"))
    pb.add_argument("-c", "--config", type=Path, default=Path("skillab.toml"))
    pb.add_argument("-o", "--out", type=Path, default=Path("skill-ab-badge.svg"))

    pp = sub.add_parser("plan", help="dry-run: project cost/time + MDE before spending")
    _cfg_args(pp)
    pp.add_argument("--cost-per-run", type=float, default=None)
    pp.add_argument("--seconds-per-run", type=float, default=None)
    pp.add_argument("--baseline", type=float, default=None)
    pp.add_argument("--noise", type=float, default=None)

    pc = sub.add_parser("ci", help="run + gate with an exit code (for CI)")
    _cfg_args(pc)
    pc.add_argument("--policy", choices=["no-regression", "require-improvement"],
                    default="no-regression")
    pc.add_argument("--html", type=Path, default=None)

    pd = sub.add_parser("demo", help="render a bundled example report+badge (offline, free)")
    pd.add_argument("-o", "--out", type=Path, default=Path("skill-ab-demo"))

    ps = sub.add_parser("serve", help="launch the local web app (uses your Claude "
                                      "subscription via `claude -p`; no API key)")
    ps.add_argument("--port", type=int, default=7878)
    ps.add_argument("--runs-dir", type=Path, default=Path.home() / ".skill-ab" / "runs")
    ps.add_argument("--no-open", action="store_true", help="don't open a browser")

    args = p.parse_args(argv)

    if args.cmd == "init":
        cmd_init(args.config)
        return 0
    if args.cmd == "serve":
        from skill_ab_server import serve   # lazy: engine has no hard dep on the server
        serve(host="127.0.0.1", port=args.port, runs_dir=args.runs_dir,
              open_browser=not args.no_open)
        return 0
    if args.cmd == "demo":
        cmd_demo(args.out)
        return 0
    if args.cmd == "run":
        cfg, tasks = _load_cfg_tasks(args)
        _run_and_outputs(cfg, tasks, resume=args.resume, html_out=args.html)
        return 0
    if args.cmd == "report":
        cfg, _tasks = load_config(args.config)
        jsonl = args.jsonl or (cfg.results_dir / "results.jsonl")
        results = load_results(jsonl)
        manifest = experiment_manifest(cfg, seed=0, timestamp=time.time())
        jp = cfg.results_dir / "judge.jsonl"          # rebuild judge charts if persisted
        comparisons = ([JudgeComparison(**json.loads(line)) for line in
                        jp.read_text().splitlines() if line.strip()] if jp.exists() else None)
        args.out.write_text(build_html_report(results, Preflight(), cfg, manifest, comparisons))
        print(f"HTML report: {args.out}")
        return 0
    if args.cmd == "badge":
        summary = json.loads(args.summary.read_text())
        cfg, _ = (load_config(args.config) if args.config.exists()
                  else (_build_example()[0], None))
        verdict = primary_verdict(summary, cfg)
        if not verdict:
            raise SystemExit(f"no ITT estimate for primary metric in {args.summary}")
        args.out.write_text(render_badge_svg(summary["primary_metric"], verdict))
        print(badge_markdown(summary["primary_metric"], verdict, str(args.out)))
        return 0
    if args.cmd == "plan":
        cfg, tasks = _load_cfg_tasks(args)
        est = estimate_cost(cfg, tasks, args.cost_per_run, args.seconds_per_run)
        cost = (f"~${est['projected_usd']:.2f}" if est["projected_usd"] is not None
                else "$? (pass --cost-per-run)")
        wall = (f"~{est['projected_wall_seconds'] / 60:.0f} min"
                if est["projected_wall_seconds"] is not None else "?")
        print(f"projected: {est['n_runs']} runs + {est['n_judge_calls']} judge calls, "
              f"{cost}, {wall} wall, ceiling {est['ceiling_usd']}")
        if args.baseline is not None and args.noise is not None:
            mde = minimum_detectable_effect(cfg, len(tasks), args.baseline, args.noise)
            print(f"minimum detectable effect at k={cfg.k}, {len(tasks)} tasks (80% power): "
                  f"{('%.0f%%' % (mde * 100)) if mde is not None else '> 50% (underpowered)'}")
        else:
            print("(pass --baseline and --noise for a minimum-detectable-effect estimate)")
        return 0
    if args.cmd == "ci":
        cfg, tasks = _load_cfg_tasks(args)
        _r, _pf, summary, _m = _run_and_outputs(cfg, tasks, html_out=args.html)
        code, msg = ci_exit_code(summary, cfg, args.policy)
        print(msg)
        return code
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
