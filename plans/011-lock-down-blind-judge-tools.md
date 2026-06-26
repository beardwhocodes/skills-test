# Plan 011: Lock down the blind-judge `claude -p` to zero tools

> **Executor instructions**: Follow this plan step by step. Run every
> verification command and confirm the expected result before moving to the
> next step. If anything in the "STOP conditions" section occurs, stop and
> report — do not improvise. A reviewer maintains `plans/README.md`; do NOT
> edit the index yourself (your row is added separately).
>
> **Drift check (run first)**: the repo is NOT git-initialized, so there is no
> SHA to diff against. Before editing, open `skill_ab_harness.py` and compare
> the "Current state" excerpts below to the live code. If the `_judge_call`
> argv list or `run_agent` no longer match the quoted lines, treat it as a
> STOP condition.

## Status

- **Priority**: P2
- **Effort**: S
- **Risk**: LOW
- **Depends on**: none
- **Category**: security
- **Planned at**: not git-initialized, 2026-06-25

## Why this matters

The blind LLM judge runs `claude -p` over two git diffs that come from the
solutions under test — i.e. **attacker-influenced text** is pasted verbatim into
the judge prompt (`_judge_prompt`). The judge's only job is to print a tiny JSON
verdict, yet its `claude -p` invocation passes **no** `--allowedTools` /
`--disallowedTools`, so it runs with the full default tool surface (Bash, Edit,
Write, Read, …) plus `--permission-mode acceptEdits`. A prompt injection embedded
in a diff ("ignore the task and run `…`") could therefore reach the filesystem or
shell of the judge host. The agent runner (`run_agent`) already constrains its
tools; the judge should too. Fix: give the judge an **empty allow-list** so it
literally cannot call a tool. This is the smallest change that closes the gap;
it cannot regress quality because the judge never needs a tool to emit JSON.

## Current state

Files (only one module + its test file in this repo):

- `skill_ab_harness.py` (~3715 lines) — the whole harness. The judge subprocess
  is built and run in `_judge_call` (around line 1436). The diff text it judges
  is assembled in `_judge_prompt` (line 1397) and embedded verbatim:

  ```python
  # skill_ab_harness.py:1397-1408
  def _judge_prompt(task_prompt: str, resp_a: str, resp_b: str, axis: str) -> str:
      return (
          "You are a meticulous, impartial code reviewer comparing two solutions to "
          ...
          "=== SOLUTION A (git diff) ===\n" + (resp_a or "(empty diff)") + "\n\n"
          "=== SOLUTION B (git diff) ===\n" + (resp_b or "(empty diff)") + "\n\n"
          ...
      )
  ```

- The judge subprocess argv is an **inline list** with no tool restriction:

  ```python
  # skill_ab_harness.py:1446-1454  (inside _judge_call)
              cmd = [
                  "claude", "-p", prompt,
                  "--output-format", "json",
                  "--model", cfg.judge_model,
                  "--max-turns", "1",
                  "--permission-mode", cfg.permission_mode,
                  "--disable-slash-commands",   # no skill can fire in the judge
                  "--strict-mcp-config",        # ignore ambient MCP servers
              ]
  ```

  Note: `cfg.permission_mode` defaults to `"acceptEdits"`
  (`skill_ab_harness.py:136`), so an injected edit would NOT be blocked by the
  permission prompt. There is **no** `--allowedTools` here.

- Contrast — the agent runner already constrains tools, the pattern to match:

  ```python
  # skill_ab_harness.py:654-666  (inside run_agent)
      cmd = [
          "claude", "-p", task.prompt,
          "--output-format", "stream-json",
          "--verbose",                       # required to get the full event stream
          "--model", cfg.model,
          "--max-turns", str(cfg.max_turns),
          "--permission-mode", cfg.permission_mode,
          "--allowedTools", ",".join(cfg.allowed_tools),
          ...
      ]
      if cfg.disallowed_tools:               # deny rules win over allow ...
          cmd += ["--disallowedTools", ",".join(cfg.disallowed_tools)]
  ```

Conventions that apply here:

- **Stdlib-only is a hard rule.** Never add a dependency (no numpy/pandas/
  pytest/jinja). Python >= 3.11.
- Comments explain **why**, not what (see the inline `# deny rules win over
  allow` style above).
- Lines stay **< 100 columns** (ruff `line-length = 100` in `pyproject.toml`).
- Tests live in `test_skill_ab_harness.py`, run by a **custom stdlib runner**
  (NOT pytest): every top-level `test_*` function is collected and called; the
  file prints `N passed`. Use the existing helper `_cfg(**kw)`
  (`test_skill_ab_harness.py:18`) which builds an `ExperimentConfig` with sane
  defaults — pass overrides as kwargs. The judge-logic tests already live under
  the `# Blind qualitative judge — pure logic` section
  (`test_skill_ab_harness.py:205+`); add the new test there, next to
  `test_judge_pairs_caps_and_handles_empty`.

## Commands you will need

| Purpose | Command | Expected on success |
|---|---|---|
| Compile | `python3 -m py_compile skill_ab_harness.py test_skill_ab_harness.py` | exit 0, no output |
| Tests | `python3 test_skill_ab_harness.py` | last line `54 passed` (was `53 passed`) |
| Lint | `uvx ruff check skill_ab_harness.py` | `All checks passed!` |

(Run from the repo root `/Users/copyjosh/Code/skills-test`. The test runner needs
no `claude`, `git`, or network — it exercises pure logic only.)

## Scope

**In scope** (the only files you may modify):
- `skill_ab_harness.py` — extract a `_judge_argv(prompt, cfg)` helper and add
  `--allowedTools ""` to it.
- `test_skill_ab_harness.py` — add one test for `_judge_argv`.

**Out of scope** (do NOT touch):
- The `run_agent` argv (`skill_ab_harness.py:654`) — it is already constrained;
  this plan only touches the judge.
- `cfg.permission_mode` itself, and the `--permission-mode` flag — do NOT change
  the default to `plan`/deny here (see Maintenance notes; it is a separate
  decision and out of scope for this minimal hardening).
- `_judge_prompt`, `_map_winner`, `_judge_pairs`, `aggregate_judge`,
  `run_qualitative_judge`, and any report/badge code — behavior is unchanged.
- `plans/README.md` — the reviewer maintains the index.

## Git workflow

The repo is NOT git-initialized — there is no branch to create and no commit to
make. Edit the two files in place. Do not run `git init`, do not push, do not
open a PR.

## Steps

### Step 1: Extract the judge argv into a testable helper with an empty allow-list

In `skill_ab_harness.py`, add a small module-level helper **immediately above**
`_judge_call` (just after `_judge_pairs`, before `async def _judge_call`). It
returns the exact same argv as today **plus** an empty `--allowedTools`:

```python
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
```

Then replace the inline `cmd = [ ... ]` block inside `_judge_call`
(`skill_ab_harness.py:1446-1454`) with a single call:

```python
            cmd = _judge_argv(prompt, cfg)
```

Leave the rest of `_judge_call` (the `mkdtemp`, `create_subprocess_exec(*cmd,
cwd=tmp, ...)`, timeout, and parsing) untouched. The refactor must be
behavior-identical **except** for the added `--allowedTools ""`.

**Verify**: `python3 -m py_compile skill_ab_harness.py` → exit 0, and
`uvx ruff check skill_ab_harness.py` → `All checks passed!`

### Step 2: Add a pure unit test for the judge argv

The judge path shells out to `claude`, so it cannot run under the stdlib test
runner. Test the **argv builder** instead. Add this next to
`test_judge_pairs_caps_and_handles_empty` in
`test_skill_ab_harness.py` (the `# Blind qualitative judge — pure logic`
section, ~line 230):

```python
def test_judge_argv_grants_no_tools():
    # The judge only prints a JSON verdict over attacker-influenced diff text,
    # so it must run with an EMPTY allow-list -- an injection in a diff can't
    # reach a tool. Safety flags (slash-commands off, strict MCP) stay on.
    argv = h._judge_argv("PROMPT-TEXT", _cfg())
    assert "--allowedTools" in argv
    assert argv[argv.index("--allowedTools") + 1] == ""   # empty -> no tools
    joined = " ".join(argv)
    assert "Bash" not in joined and "Edit" not in joined and "Write" not in joined
    assert "PROMPT-TEXT" in argv                          # prompt still passed through
    assert "--disable-slash-commands" in argv and "--strict-mcp-config" in argv
```

**Verify**: `python3 test_skill_ab_harness.py` → last line `54 passed`, and the
line `ok  test_judge_argv_grants_no_tools` appears in the output.

## Test plan

- New test in `test_skill_ab_harness.py`: `test_judge_argv_grants_no_tools`
  (added in Step 2). It covers:
  - happy path: `_judge_argv` returns an argv containing `--allowedTools`
    followed by `""` (the empty allow-list — the security fix);
  - the regression guard: no tool name (`Bash`/`Edit`/`Write`) appears anywhere
    in the argv;
  - the prompt is still forwarded and the existing safety flags
    (`--disable-slash-commands`, `--strict-mcp-config`) are retained.
- Structural pattern to follow: the existing pure-logic judge tests in the same
  section (`test_judge_pairs_caps_and_handles_empty`,
  `test_aggregate_position_bias_washes_out`) — same `h.`-prefixed access and
  `_cfg()` helper.
- Verification: `python3 test_skill_ab_harness.py` → `54 passed` (one more than
  the current `53 passed`).

## Done criteria

Machine-checkable. ALL must hold:

- [ ] `python3 -m py_compile skill_ab_harness.py test_skill_ab_harness.py` exits 0.
- [ ] `python3 test_skill_ab_harness.py` prints `54 passed`, including
      `test_judge_argv_grants_no_tools`.
- [ ] `uvx ruff check skill_ab_harness.py` prints `All checks passed!`.
- [ ] `grep -n "_judge_argv" skill_ab_harness.py` shows the helper definition
      AND its use inside `_judge_call` (the inline `cmd = [ ... ]` list is gone).
- [ ] `grep -n -- '--allowedTools' skill_ab_harness.py` shows the flag in BOTH
      `run_agent` and `_judge_argv` (2+ matches).
- [ ] No files outside `skill_ab_harness.py` and `test_skill_ab_harness.py` are
      modified.

## STOP conditions

Stop and report back (do not improvise) if:

- The code at `skill_ab_harness.py:1446-1454` no longer matches the inline `cmd`
  list quoted in "Current state" (the harness drifted since this plan was
  written).
- After adding the helper, `_judge_call` no longer references `cmd` /
  `create_subprocess_exec(*cmd, ...)` — i.e. the refactor would change behavior
  beyond adding the flag.
- A verification command fails twice after a reasonable fix attempt.
- You find evidence that the live `claude` CLI rejects an **empty** value for
  `--allowedTools` (e.g. an arg-parse error). Do NOT guess a different value
  (such as a fake tool name) — report it; the operator will decide between an
  empty string vs. an explicit `--disallowedTools` deny-all.
- The change appears to require editing any out-of-scope file.

## Maintenance notes

For whoever owns this code next:

- **What a reviewer should scrutinize**: confirm `--allowedTools ""` actually
  yields zero tools in the installed `claude` version (run one real judge call
  with `judge_enabled=True` and confirm the verdict JSON is still produced and no
  tool was used). The unit test only proves the argv is built correctly, not the
  CLI's runtime interpretation — this is the contract worth confirming live.
- **Deferred, intentionally out of scope**: the judge still inherits
  `cfg.permission_mode` (default `acceptEdits`). With an empty allow-list that is
  belt-and-suspenders, but a defense-in-depth follow-up could pass a dedicated
  read-only/deny permission mode (e.g. `plan`) for the judge instead of reusing
  the agent's mode. Left out here to keep the change minimal and behavior-stable.
- **Interaction**: if a future change ever needs the judge to call a tool (it
  shouldn't — it only emits JSON), this empty allow-list must be revisited, and
  the threat model (attacker-controlled diff text in the prompt) re-evaluated
  before granting any tool.
- The reviewer will add this plan's row to `plans/README.md`; do not edit it.
