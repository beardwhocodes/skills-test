# Plan 001: A real CLI + TOML config so nobody hand-edits the script

> **Executor instructions**: Follow this plan step by step. Run every verification
> command and confirm the expected result before moving on. If anything in "STOP
> conditions" occurs, stop and report — do not improvise. When done, update this
> plan's status row in `plans/README.md`.
>
> **Drift check (run first)**: the repo is not git-tracked. Open
> `skill_ab_harness.py` and confirm the "Current state" excerpts below still
> match (especially the `if __name__ == "__main__"` block and `ExperimentConfig`/
> `Task` dataclasses). On a mismatch, treat it as a STOP condition.

## Status

- **Priority**: P1
- **Effort**: M
- **Risk**: LOW
- **Depends on**: none
- **Category**: dx
- **Planned at**: 2026-06-25 (repo not git-initialized)

## Why this matters

Today the only way to run the harness is to hand-edit Python-literal config at the
bottom of a 1300-line file (`/path/to/your/repo` placeholders). That is the single
biggest adoption blocker: there is no `--help`, no config file, no install entry
point. This plan adds an `argparse` CLI (`init` / `run`) backed by a stdlib
`tomllib` config, plus a `[project.scripts]` entry so `skills-test` is a command. The
existing Python-literal path keeps working. After this, the try-now story is
`uvx skills-test init && skills-test run`.

## Current state

- `skill_ab_harness.py` — the whole harness in one file. Relevant pieces:
  - `class Task` (lines 88–101, `@dataclass(frozen=True)`): fields `id`, `prompt`,
    `setup_cmd`, `test_cmd`, `lint_cmd`, `build_cmd`.
  - `class ExperimentConfig` (lines 104–142, `@dataclass(frozen=True)`): fields
    incl. `repo_path: Path`, `base_ref: str`, `skill_src: Path`, `skill_name: str`,
    `model`, `k`, `max_concurrency`, etc. (all the run knobs).
  - The entry point, currently hand-edited literals:
    ```python
    # skill_ab_harness.py:1301
    if __name__ == "__main__":
        cfg = ExperimentConfig(
            repo_path=Path("/path/to/your/repo"),
            base_ref="main",
            skill_src=Path("/path/to/skills/my-skill"),
            skill_name="my-skill",
            k=5,
        )
        tasks = [ Task(id="add-pagination", prompt="...", setup_cmd="npm ci --silent", ...) ]
        results, pf = asyncio.run(run_experiment(cfg, tasks))
        print(build_report(results, pf, cfg))
    ```
- `pyproject.toml` — has `[project]` and `[tool.ruff]` but **no** `[project.scripts]`.
  `requires-python = ">=3.11"`, so `tomllib` (stdlib since 3.11) is available.
- Convention: stdlib-only (`dependencies = []` by design). Frozen dataclasses.
  Comments explain *why*. Match this.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Compile | `python3 -m py_compile skill_ab_harness.py test_skill_ab_harness.py` | exit 0, no output |
| Unit tests | `python3 test_skill_ab_harness.py` | last line `N passed` (N grows by the new tests) |
| Line length | see Step 5 snippet | `line-length OK` |
| CLI smoke | `python3 skill_ab_harness.py --help` | prints usage with `init` and `run` |

## Scope

**In scope:**
- `skill_ab_harness.py` (add imports, `load_config`, `cmd_init`, `main`, rewrite `__main__`)
- `test_skill_ab_harness.py` (add config round-trip tests)
- `pyproject.toml` (add `[project.scripts]`)

**Out of scope (do NOT touch):**
- Any statistics, scorer, worktree, activation, or judge logic. This plan only adds
  an input layer that builds the SAME `ExperimentConfig`/`Task` objects.
- Do not change the field names or defaults of `ExperimentConfig`/`Task`.

## Steps

### Step 1: Add the config loader

Add near the other module-level helpers (after the dataclasses, before `Scorer`):

```python
import tomllib  # add to the stdlib import block at the top

def load_config(config_path: Path) -> tuple[ExperimentConfig, list[Task]]:
    """Build the experiment from a TOML file. The [experiment] table maps to
    ExperimentConfig fields (Path-typed fields accept strings); [[task]] tables
    map to Task. Unknown keys raise, so typos fail loud."""
    data = tomllib.loads(config_path.read_text())
    exp = dict(data.get("experiment", {}))
    for k in ("repo_path", "skill_src", "worktree_root", "results_dir"):
        if k in exp:
            exp[k] = Path(exp[k]).expanduser()
    cfg = ExperimentConfig(**exp)  # frozen dataclass; unknown key -> TypeError
    tasks = [Task(**t) for t in data.get("task", [])]
    if not tasks:
        raise ValueError(f"{config_path}: no [[task]] tables found")
    return cfg, tasks
```

**Verify**: `python3 -c "import skill_ab_harness"` → exit 0.

### Step 2: Add `init` to scaffold config from the cwd

```python
_INIT_TEMPLATE = '''# skills-test experiment — edit then run `skills-test run -c {name}`
[experiment]
repo_path  = "{repo}"      # a git repo (worktrees fork from base_ref)
base_ref   = "main"
skill_src  = "{skill_src}" # the SKILL.md folder under test
skill_name = "{skill_name}"
k          = 5             # runs per arm per task (k=1 proves nothing)

[[task]]
id       = "example-task"
prompt   = "Describe a real change you'd ask the agent to make, with tests."
setup_cmd = "npm ci --silent"   # REQUIRED for repos with deps; a fresh worktree has none
test_cmd  = "npm test --silent"
lint_cmd  = "npm run lint --silent"
build_cmd = "npm run build --silent"
'''

def cmd_init(out_path: Path) -> None:
    """Write a starter skillab.toml, pre-filled from the cwd: detect a git repo
    and the first .claude/skills/<name>/ if present."""
    cwd = Path.cwd()
    skills = sorted(cwd.glob(".claude/skills/*/SKILL.md"))
    skill_dir = skills[0].parent if skills else Path("./.claude/skills/my-skill")
    text = _INIT_TEMPLATE.format(
        name=out_path.name, repo=str(cwd),
        skill_src=str(skill_dir), skill_name=skill_dir.name)
    if out_path.exists():
        raise SystemExit(f"{out_path} already exists; refusing to overwrite")
    out_path.write_text(text)
    print(f"wrote {out_path} — edit it, then: skills-test run -c {out_path}")
```

**Verify**: in a scratch dir, `python3 .../skill_ab_harness.py init -c /tmp/x.toml`
then `cat /tmp/x.toml` shows the template with the cwd path filled in.

### Step 3: Add the argparse `main` and `run` command

Replace the entire `if __name__ == "__main__":` block (lines 1301–end) with:

```python
def _build_example():
    """The original hand-edited example, kept as `run --example`."""
    cfg = ExperimentConfig(
        repo_path=Path("/path/to/your/repo"), base_ref="main",
        skill_src=Path("/path/to/skills/my-skill"), skill_name="my-skill", k=5)
    tasks = [Task(id="add-pagination",
                  prompt="Add cursor-based pagination to /items, with tests.",
                  setup_cmd="npm ci --silent", test_cmd="npm test --silent",
                  lint_cmd="npm run lint --silent", build_cmd="npm run build --silent")]
    return cfg, tasks

def main(argv: list[str] | None = None) -> int:
    import argparse
    p = argparse.ArgumentParser(prog="skills-test", description=__doc__.splitlines()[1] if __doc__ else "")
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("init", help="scaffold a skillab.toml in the current repo")
    pi.add_argument("-c", "--config", type=Path, default=Path("skillab.toml"))
    pr = sub.add_parser("run", help="run the A/B experiment from a config")
    pr.add_argument("-c", "--config", type=Path, default=Path("skillab.toml"))
    pr.add_argument("--example", action="store_true", help="run the built-in example config")
    args = p.parse_args(argv)
    if args.cmd == "init":
        cmd_init(args.config)
        return 0
    if args.cmd == "run":
        cfg, tasks = _build_example() if args.example else load_config(args.config)
        results, pf = asyncio.run(run_experiment(cfg, tasks))
        print(build_report(results, pf, cfg))
        if cfg.judge_enabled:
            comparisons = asyncio.run(run_qualitative_judge(results, tasks, cfg))
            print("\n" + build_judge_report(comparisons, cfg, results))
        return 0
    return 2

if __name__ == "__main__":
    raise SystemExit(main())
```

**Verify**: `python3 skill_ab_harness.py --help` lists `init` and `run`;
`python3 skill_ab_harness.py run --help` shows `--example`.

### Step 4: Register the console script

In `pyproject.toml`, after `[project]`, add:

```toml
[project.scripts]
skill-ab = "skill_ab_harness:main"
```

(For `uvx`/`pipx` the module must be importable as `skill_ab_harness`; it already
is — a single top-level module. No package restructure needed.)

**Verify**: `python3 -c "import tomllib, skill_ab_harness as h; assert callable(h.main)"`.

### Step 5: Tests + line length

Add to `test_skill_ab_harness.py`:

```python
def test_load_config_round_trips(tmp_path=None):
    import tempfile, pathlib
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "c.toml").write_text(
        '[experiment]\n'
        'repo_path="/r"\nbase_ref="main"\nskill_src="/s/k"\nskill_name="k"\nk=3\n'
        '[[task]]\nid="t1"\nprompt="do x"\ntest_cmd="pytest"\n')
    cfg, tasks = h.load_config(d / "c.toml")
    assert cfg.skill_name == "k" and cfg.k == 3
    assert str(cfg.repo_path) == "/r"      # Path-coerced
    assert len(tasks) == 1 and tasks[0].test_cmd == "pytest"

def test_load_config_rejects_unknown_key():
    import tempfile, pathlib
    d = pathlib.Path(tempfile.mkdtemp())
    (d / "c.toml").write_text('[experiment]\nrepo_path="/r"\nbase_ref="m"\n'
                              'skill_src="/s"\nskill_name="k"\nbogus=1\n[[task]]\nid="t"\nprompt="p"\n')
    try:
        h.load_config(d / "c.toml"); assert False, "should have raised"
    except TypeError:
        pass
```

Line-length gate:
```bash
python3 - <<'PY'
import sys
bad=[(f,i,len(l.rstrip())) for f in ("skill_ab_harness.py","test_skill_ab_harness.py")
     for i,l in enumerate(open(f),1) if len(l.rstrip())>100]
[print("LONG",*b) for b in bad]; print("line-length", "OK" if not bad else "FAIL"); sys.exit(1 if bad else 0)
PY
```

**Verify**: `python3 test_skill_ab_harness.py` → `N passed` (2 new tests); line-length `OK`.

## Test plan

- `test_load_config_round_trips` — happy path: TOML → `ExperimentConfig` + `Task`,
  with `Path` coercion of `repo_path`.
- `test_load_config_rejects_unknown_key` — a typo'd key raises (loud failure),
  proving the frozen-dataclass `**kwargs` guard works.
- Model after the existing tests in `test_skill_ab_harness.py` (plain asserts,
  `_run_all()` discovers `test_*`).

## Done criteria

- [ ] `python3 -m py_compile skill_ab_harness.py test_skill_ab_harness.py` exits 0
- [ ] `python3 skill_ab_harness.py --help` shows `init` and `run`
- [ ] `python3 skill_ab_harness.py init -c /tmp/x.toml` writes a template; re-running refuses to overwrite
- [ ] `python3 test_skill_ab_harness.py` passes incl. 2 new config tests
- [ ] `pyproject.toml` has `[project.scripts] skill-ab = "skill_ab_harness:main"`
- [ ] line-length gate prints `OK`
- [ ] `plans/README.md` status row for 001 updated

## STOP conditions

- The `__main__` block or `ExperimentConfig`/`Task` fields don't match the
  excerpts above (file drifted) — stop and report.
- `import tomllib` fails (Python < 3.11) — stop; the project requires 3.11+.
- Building `ExperimentConfig(**exp)` needs a field rename to work — stop; you are
  out of scope (don't change the dataclass).

## Maintenance notes

- When new `ExperimentConfig`/`Task` fields are added later, they become TOML keys
  for free (no loader change) — but update `_INIT_TEMPLATE` if a new field is
  commonly needed.
- Reviewer: confirm `--example` still reproduces the old hand-edited behavior so
  existing muscle memory isn't broken.
- Plans 005 (`--demo`) and 006 (`--plan`) add more subcommands to this same
  `main()` — keep the subparser structure easy to extend.
