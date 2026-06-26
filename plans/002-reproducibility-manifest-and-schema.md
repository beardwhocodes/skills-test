# Plan 002: Reproducibility manifest + portable `summary.json` schema

> **Executor instructions**: Follow step by step; run each verification before
> moving on. Honor STOP conditions. Update this plan's row in `plans/README.md`
> when done.
>
> **Drift check (run first)**: repo is not git-tracked. Open `skill_ab_harness.py`
> and confirm the "Current state" excerpts (`build_report` header, `DiffEstimate`,
> `run_experiment`) still match before editing. Mismatch → STOP.

## Status

- **Priority**: P1
- **Effort**: S–M
- **Risk**: LOW
- **Depends on**: none (but 003/004/005/006 depend on THIS)
- **Category**: direction / credibility
- **Planned at**: 2026-06-25 (repo not git-initialized)

## Why this matters

A result is currently a stdout Markdown blob with almost no provenance. A
skeptic's first move is "which model? which commit of the repo? what seed? which
CLI version?" — and the report can't answer. CLAUDE.md itself notes the CLI
version is load-bearing for activation-detector validity, yet it's absent from
output. This plan captures a **reproducibility manifest** and writes a portable,
versioned **`summary.json`** (manifest + the per-metric `DiffEstimate`s). The
manifest kills the "which X?" dismissal; the JSON is the substrate the badge
(003), the HTML report (004), the demo (005), and any future leaderboard share.

## Current state

- `build_report(results, pf, cfg, scorers=None, seed=0)` (`skill_ab_harness.py:928`)
  — returns a Markdown string. Its header today (around lines 950–956) prints only:
  ```python
  f"# Skill A/B report — `{cfg.skill_name}`",
  f"model: {cfg.model} | k: {cfg.k}/arm/task | bootstrap: {cfg.bootstrap_iters:,} ...",
  ```
- `class DiffEstimate` (`skill_ab_harness.py:765`, `@dataclass`) — fields:
  `metric, mean_on, mean_off, point, ci_low, ci_high, p_value, n_on, n_off,
  n_tasks, clustered, q_value`. Already serialization-ready (all scalars).
- `estimate_diff(results, metric, mode, cfg, rng)` (`:876`) returns a
  `DiffEstimate | None`. `build_report` calls it per metric for `"itt"` and `"pp"`.
- `run_experiment` (`:725`) writes `results_dir/results.jsonl`; `cfg.results_dir`
  exists (default `/tmp/skill_ab_results`).
- `cfg.repo_path`, `cfg.base_ref`, `cfg.skill_src`, `cfg.skill_name`, `cfg.model`,
  `cfg.bootstrap_iters`, `cfg.permutation_iters`, `cfg.bootstrap_alpha` all exist.
- `_git(repo, args)` (`:279`) runs `git -C repo …` and returns a CompletedProcess.
- `pyproject.toml` has `version = "0.1.0"`.
- Convention: stdlib only. Use `hashlib`, `subprocess`, `platform`, `json`.

## Commands you will need

| Purpose | Command | Expected |
|---|---|---|
| Compile | `python3 -m py_compile skill_ab_harness.py test_skill_ab_harness.py` | exit 0 |
| Tests | `python3 test_skill_ab_harness.py` | `N passed` |
| Line length | the `python3 - <<'PY' …` snippet from plan 001 Step 5 | `OK` |

## Scope

**In scope:**
- `skill_ab_harness.py` — add `__version__`, `experiment_manifest()`,
  `summary_dict()`, `write_summary()`, and a manifest block in `build_report`.
- `test_skill_ab_harness.py` — tests for `summary_dict` shape + manifest keys.
- `results.schema.json` (new file at repo root) — the JSON Schema for `summary.json`.

**Out of scope:**
- The statistics themselves (`estimate_diff`, bootstrap, permutation, BH). Read
  their outputs; do not change them.
- The Markdown table bodies — only ADD a manifest header block.

## Steps

### Step 1: Add a version constant

Near the top of `skill_ab_harness.py` (after the docstring/imports):
```python
__version__ = "0.1.0"   # keep in sync with pyproject.toml
```
**Verify**: `python3 -c "import skill_ab_harness as h; print(h.__version__)"` → `0.1.0`.

### Step 2: Build the manifest

```python
def experiment_manifest(cfg: ExperimentConfig, seed: int = 0,
                        timestamp: float | None = None) -> dict:
    """Provenance for a run. Best-effort: external calls that fail record None,
    never raise. `timestamp` is injected (do not call time.time() implicitly here
    so reports are reproducible in tests)."""
    import platform
    def _try(fn):
        try:
            return fn()
        except Exception:  # noqa: BLE001
            return None
    cli_version = _try(lambda: subprocess.run(
        ["claude", "--version"], capture_output=True, text=True, timeout=30).stdout.strip())
    base_sha = _try(lambda: _git(cfg.repo_path, ["rev-parse", cfg.base_ref]).stdout.strip())
    skill_md = cfg.skill_src / "SKILL.md"
    skill_hash = _try(lambda: hashlib.sha256(skill_md.read_bytes()).hexdigest())
    return {
        "harness_version": __version__,
        "claude_cli_version": cli_version,
        "model": cfg.model,
        "repo_path": str(cfg.repo_path),
        "base_ref": cfg.base_ref,
        "base_ref_sha": base_sha,
        "skill_name": cfg.skill_name,
        "skill_md_sha256": skill_hash,
        "k": cfg.k,
        "seed": seed,
        "bootstrap_iters": cfg.bootstrap_iters,
        "permutation_iters": cfg.permutation_iters,
        "alpha": cfg.bootstrap_alpha,
        "platform": platform.platform(),
        "timestamp": timestamp,
    }
```
Add `import hashlib` to the import block if not present.

**Verify**:
```bash
python3 -c "
import skill_ab_harness as h; from pathlib import Path
cfg=h.ExperimentConfig(repo_path=Path('/r'),base_ref='main',skill_src=Path('/s/k'),skill_name='k')
m=h.experiment_manifest(cfg, seed=7, timestamp=123.0)
assert m['harness_version']=='0.1.0' and m['seed']==7 and m['timestamp']==123.0
assert set(['model','base_ref_sha','skill_md_sha256','claude_cli_version']) <= set(m)
print('manifest ok')"
```

### Step 3: Build `summary_dict` and `write_summary`

```python
def summary_dict(results: list[RunResult], cfg: ExperimentConfig,
                 manifest: dict, scorers: list[Scorer] | None = None,
                 seed: int = 0) -> dict:
    """Portable, schema-versioned summary: manifest + per-metric ITT/PP estimates
    + validity/activation counts. Reuses the SAME estimators build_report uses."""
    import random
    scorers = scorers or default_scorers(cfg)
    rng = random.Random(seed)
    metrics = [s.name for s in scorers] + ["cost_usd"]

    def estimates(mode):
        out = {}
        for m in metrics:
            e = estimate_diff(results, m, mode, cfg, rng)
            if e:
                out[m] = {k: getattr(e, k) for k in (
                    "mean_on", "mean_off", "point", "ci_low", "ci_high",
                    "p_value", "q_value", "n_on", "n_off", "n_tasks", "clustered")}
        return out

    on_fire, on_clean = activation_rate(results, Arm.SKILL_ON)
    off_fire, off_clean = activation_rate(results, Arm.SKILL_OFF)
    return {
        "schema_version": 1,
        "manifest": manifest,
        "primary_metric": cfg.primary_metric,
        "itt": estimates("itt"),
        "per_protocol": estimates("pp"),
        "validity": {
            "on_valid": sum(1 for r in results if r.arm is Arm.SKILL_ON and r.itt_valid),
            "off_valid": sum(1 for r in results if r.arm is Arm.SKILL_OFF and r.itt_valid),
            "on_activation_fired": on_fire, "on_activation_clean": on_clean,
            "off_contaminated": off_fire,
        },
    }

def write_summary(results: list[RunResult], cfg: ExperimentConfig, manifest: dict,
                  seed: int = 0) -> Path:
    cfg.results_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.results_dir / "summary.json"
    path.write_text(json.dumps(summary_dict(results, cfg, manifest, seed=seed), indent=2))
    return path
```

**Verify**: a unit test (Step 6) builds `summary_dict` from synthetic `RunResult`s
and asserts `schema_version`, `manifest`, `itt`, `validity` keys exist.

### Step 4: Put a manifest block in the Markdown report

In `build_report`, give it access to a manifest. Add an optional parameter and a
header block. Change the signature:
```python
def build_report(results, pf, cfg, scorers=None, seed=0, manifest=None):
```
and right after the existing first two header lines, insert:
```python
    if manifest is None:
        manifest = experiment_manifest(cfg, seed=seed)
    L += [
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
    ]
```
(Place this insertion immediately after the existing header lines and before the
`## Validity` section. `L` is the running list of report lines.)

**Verify**: `python3 skill_ab_harness.py run --example` (from plan 001) — the
printed report now contains a "reproducibility manifest" block. If plan 001 isn't
landed yet, call `build_report(results, pf, cfg)` directly in a REPL.

### Step 5: Ship the JSON Schema

Create `results.schema.json` at the repo root: a JSON Schema (draft 2020-12)
describing the `summary_dict` output — `schema_version` (const 1), `manifest`
(object), `primary_metric` (string), `itt`/`per_protocol` (objects whose values
have the `DiffEstimate` numeric fields), `validity` (object of ints). Keep it in
sync with Step 3's structure.

**Verify**:
```bash
python3 -c "import json; json.load(open('results.schema.json')); print('schema parses')"
```

### Step 6: Tests

```python
def test_manifest_and_summary_shape():
    from pathlib import Path
    cfg = _cfg()  # helper already in the test file
    man = h.experiment_manifest(cfg, seed=3, timestamp=1.0)
    assert man["seed"] == 3 and man["harness_version"]
    # synthetic results: 2 tasks, both arms, one metric
    res = []
    for t in ("A", "B"):
        for arm, v in ((Arm.SKILL_ON, 1.0), (Arm.SKILL_OFF, 0.0)):
            r = _rr(arm, arm is Arm.SKILL_ON, scores={"tests_pass": v}); r.task_id = t
            res.append(r)
    s = h.summary_dict(res, cfg, man)
    assert s["schema_version"] == 1 and s["primary_metric"] == "tests_pass"
    assert "tests_pass" in s["itt"] and "ci_low" in s["itt"]["tests_pass"]
    assert set(["on_valid", "off_valid", "off_contaminated"]) <= set(s["validity"])
```
(`_cfg` and `_rr` already exist in `test_skill_ab_harness.py`; reuse them.)

**Verify**: `python3 test_skill_ab_harness.py` → `N passed` (1 new test); line-length `OK`.

## Test plan

- `test_manifest_and_summary_shape` — manifest carries the injected seed/timestamp
  and required keys; `summary_dict` has `schema_version`, per-metric ITT estimate
  with CI fields, and validity counts. Model after existing tests (plain asserts).
- No test asserts on `claude --version` / `git` output (external, may be absent) —
  the manifest's `_try` returns None there, which is the intended behavior.

## Done criteria

- [ ] `python3 -m py_compile …` exits 0
- [ ] `experiment_manifest` returns the keys in Step 2's verify block
- [ ] `summary_dict` has `schema_version`, `manifest`, `itt`, `per_protocol`, `validity`
- [ ] `results.schema.json` exists and parses
- [ ] report contains a "reproducibility manifest" block
- [ ] `python3 test_skill_ab_harness.py` passes incl. the new test
- [ ] line-length `OK`
- [ ] `plans/README.md` row for 002 updated

## STOP conditions

- `DiffEstimate` field names differ from the excerpt — stop; downstream getattrs
  will silently drop fields.
- `build_report`'s line-list variable is not `L` or the header structure differs —
  stop and report (don't guess where to insert the manifest block).

## Maintenance notes

- Bump `schema_version` whenever `summary_dict`'s shape changes incompatibly, and
  update `results.schema.json` in the same change — downstream consumers (badge,
  HTML, any leaderboard) key off it.
- Keep `__version__` and `pyproject.toml`'s `version` in sync (consider a test
  asserting they match).
- Reviewer: confirm no secret/credential could land in the manifest (it captures
  paths and hashes only — never file contents).
