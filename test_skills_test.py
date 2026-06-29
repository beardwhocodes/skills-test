"""
Deterministic unit tests for the pure logic of skills_test.

No `claude`, no `git`, no network: these exercise the statistics, the ITT/PP
validity rules, and the rewritten activation detector against synthetic events.
Run: `python3 -m pytest -q` or `python3 test_skills_test.py`.
"""

from __future__ import annotations

import random
from pathlib import Path

import skills_test as h
from skills_test import Arm, ExperimentConfig, RunResult


def _cfg(**kw) -> ExperimentConfig:
    base = dict(
        repo_path=Path("/repo"), base_ref="main",
        skill_src=Path("/skills/my-skill"), skill_name="my-skill",
        bootstrap_iters=2000, permutation_iters=2000,
    )
    base.update(kw)
    return ExperimentConfig(**base)


def _assistant(tool: str, inp: dict) -> dict:
    return {"type": "assistant",
            "message": {"content": [{"type": "tool_use", "name": tool, "input": inp}]}}


def _result_ev() -> dict:
    return {"type": "result", "is_error": False, "total_cost_usd": 0.01, "num_turns": 3}


# --------------------------------------------------------------------------
# Validity rules (ITT vs per-protocol)  -- stats-1
# --------------------------------------------------------------------------

def _rr(arm: Arm, activated, agent_ok=True, **kw) -> RunResult:
    kw.setdefault("arm_skill_name", None if arm is Arm.SKILL_OFF else "my-skill")
    return RunResult(task_id="t", arm=arm, run_index=0, worktree=Path("/wt"),
                     skill_activated=activated, activation_reason="", agent_ok=agent_ok,
                     completed=True, **kw)


def test_itt_keeps_non_activated_on_run():
    # The whole point of ITT: a clean ON run that did NOT fire the skill is STILL
    # valid (no post-treatment conditioning).
    assert _rr(Arm.SKILL_ON, False).itt_valid is True
    assert _rr(Arm.SKILL_ON, True).itt_valid is True
    # ...but per-protocol drops it.
    assert _rr(Arm.SKILL_ON, False).pp_valid is False
    assert _rr(Arm.SKILL_ON, True).pp_valid is True


def test_off_contamination_excluded_in_both_modes():
    contaminated = _rr(Arm.SKILL_OFF, None, contaminated_by="my-skill")  # a skill leaked in
    assert contaminated.contaminated is True
    assert contaminated.itt_valid is False
    assert contaminated.pp_valid is False
    clean_off = _rr(Arm.SKILL_OFF, None)
    assert clean_off.itt_valid is True and clean_off.pp_valid is True


def test_head_to_head_validity_and_contamination():
    # skill_b arm: valid when its own skill fired and no foreign skill leaked.
    b = _rr(Arm.SKILL_B, True, arm_skill_name="skill-b")
    assert b.itt_valid is True and b.pp_valid is True
    # skill_b run contaminated by skill A firing -> excluded from both estimands.
    bc = _rr(Arm.SKILL_B, True, arm_skill_name="skill-b", contaminated_by="skill-a")
    assert bc.contaminated is True and bc.itt_valid is False and bc.pp_valid is False


def test_failed_agent_never_valid():
    assert _rr(Arm.SKILL_ON, None, agent_ok=False).itt_valid is False


# --------------------------------------------------------------------------
# Permutation p-value  -- stats-2 (all-ties) and stats-3 (null-calibrated)
# --------------------------------------------------------------------------

def test_permutation_p_is_one_when_both_arms_constant():
    # The degenerate case that made the old code print p=0.000 (max significance)
    # for a true-zero effect. Must now be ~1.0.
    on = {"a": [1.0] * 5, "b": [1.0] * 5}
    off = {"a": [1.0] * 5, "b": [1.0] * 5}
    p = h.cluster_permutation_p(on, off, ["a", "b"], 0.0, 2000, random.Random(0))
    assert p == 1.0


def test_permutation_p_small_for_strong_effect():
    on = {"a": [1.0] * 8, "b": [1.0] * 8}
    off = {"a": [0.0] * 8, "b": [0.0] * 8}
    point = 1.0   # pooled mean diff of all-1 ON vs all-0 OFF
    p = h.cluster_permutation_p(on, off, ["a", "b"], point, 5000, random.Random(1))
    assert p < 0.01
    assert p > 0.0          # +1 smoothing: never exactly zero


# --------------------------------------------------------------------------
# Point / CI / p share ONE task set  -- stats-5 / stats-8
# --------------------------------------------------------------------------

def test_estimate_uses_only_shared_tasks_for_point_and_ci():
    rng = random.Random(0)
    cfg = _cfg()
    results = []
    # task A: valid in both arms. task B: ON-only (no OFF). B must be ignored.
    for v in (1.0, 1.0, 1.0):
        results.append(_rr(Arm.SKILL_ON, True, scores={"tests_pass": v}))
        results[-1].task_id = "A"
    for v in (1.0, 1.0, 1.0):
        results.append(_rr(Arm.SKILL_OFF, False, scores={"tests_pass": v}))
        results[-1].task_id = "A"
    for v in (0.0, 0.0):
        results.append(_rr(Arm.SKILL_ON, True, scores={"tests_pass": v}))
        results[-1].task_id = "B"
    est = h.estimate_diff(results, "tests_pass", "itt", cfg, rng)
    assert est is not None
    # Only task A is shared -> delta is 1.0 - 1.0 = 0, NOT dragged down by B's 0s.
    assert est.point == 0.0
    assert est.n_tasks == 1
    # The point estimate lies within its own CI (the stats-5 invariant).
    assert est.ci_low <= est.point <= est.ci_high


# --------------------------------------------------------------------------
# Benjamini-Hochberg  -- stats-4
# --------------------------------------------------------------------------

def test_benjamini_hochberg_monotone_and_bounded():
    q = h.benjamini_hochberg([0.01, 0.02, 0.03, 0.04, 0.05])
    assert all(0.0 <= x <= 1.0 for x in q)
    # smallest raw p gets the largest inflation factor (m/1); all q >= their p
    assert all(qi >= pi - 1e-9 for qi, pi in zip(q, [0.01, 0.02, 0.03, 0.04, 0.05]))
    assert h.benjamini_hochberg([]) == []


# --------------------------------------------------------------------------
# Activation detection  -- skill-1 / activation-3
# --------------------------------------------------------------------------

def test_skill_tool_exact_name_match():
    cfg = _cfg(skill_name="test")
    wt = Path("/wt")
    # exact match fires
    ev = [_assistant("Skill", {"name": "test"}), _result_ev()]
    assert h.detect_activation(ev, wt, cfg.skill_name)[0] is True
    # substring collision must NOT fire ('test' must not match 'test-runner')
    ev = [_assistant("Skill", {"name": "test-runner"}), _result_ev()]
    assert h.detect_activation(ev, wt, cfg.skill_name)[0] is False


def test_local_skill_md_read_fires_global_does_not(tmp_path: Path | None = None):
    cfg = _cfg(skill_name="my-skill")
    wt = Path("/private/var/wt")            # a concrete absolute worktree
    local = "/private/var/wt/.claude/skills/my-skill/SKILL.md"
    glob = "/Users/somebody/.claude/skills/my-skill/SKILL.md"
    # Read of the worktree-local copy -> activation
    ev = [_assistant("Read", {"file_path": local}), _result_ev()]
    assert h.detect_activation(ev, wt, cfg.skill_name)[0] is True
    # Read of a GLOBAL copy (different abs path) -> NOT local activation
    ev = [_assistant("Read", {"file_path": glob}), _result_ev()]
    assert h.detect_activation(ev, wt, cfg.skill_name)[0] is False
    # relative path resolves against the worktree cwd -> activation
    ev = [_assistant("Bash",
                     {"command": "cat .claude/skills/my-skill/SKILL.md"}), _result_ev()]
    assert h.detect_activation(ev, wt, cfg.skill_name)[0] is True


def test_incidental_path_mention_does_not_fire():
    cfg = _cfg(skill_name="my-skill")
    wt = Path("/private/var/wt")
    # The path appears only in a Write's CONTENT (not file_path) -> no false positive
    ev = [_assistant("Write", {"file_path": "/private/var/wt/notes.txt",
                               "content": "see .claude/skills/my-skill/SKILL.md"}),
          _result_ev()]
    assert h.detect_activation(ev, wt, cfg.skill_name)[0] is False


def test_none_on_empty_and_truncated_streams():
    cfg = _cfg()
    wt = Path("/wt")
    assert h.detect_activation([], wt, cfg.skill_name)[0] is None          # no events
    # events but no terminal result event -> truncated -> undeterminable
    ev = [_assistant("Read", {"file_path": "/wt/other.txt"})]
    assert h.detect_activation(ev, wt, cfg.skill_name)[0] is None


# --------------------------------------------------------------------------
# Scorer timeout wiring  -- async-5 (no longer dead config)
# --------------------------------------------------------------------------

def test_scorer_timeout_threaded_from_config():
    scorers = h.default_scorers(_cfg(scorer_timeout_s=123))
    assert all(getattr(s, "timeout", None) == 123 for s in scorers)
    # default (no cfg) keeps the historical 600s
    assert all(getattr(s, "timeout", None) == 600 for s in h.default_scorers())


# --------------------------------------------------------------------------
# Blind qualitative judge — pure logic (open item #2)
# --------------------------------------------------------------------------

def test_blinding_map_unwinds_ordering_correctly():
    # In a_first, slot "A" held a_label's diff; in b_first, slot "A" held b_label's.
    assert h._map_winner("a_first", "A", "alpha", "beta") == "alpha"
    assert h._map_winner("a_first", "B", "alpha", "beta") == "beta"
    assert h._map_winner("b_first", "A", "alpha", "beta") == "beta"
    assert h._map_winner("b_first", "B", "alpha", "beta") == "alpha"
    assert h._map_winner("a_first", "tie", "alpha", "beta") == "tie"
    assert h._map_winner("a_first", "garbage", "alpha", "beta") is None


def test_extract_json_tolerates_fences_and_prose():
    assert h._extract_json('{"winner":"A"}')["winner"] == "A"
    assert h._extract_json('```json\n{"winner":"B"}\n```')["winner"] == "B"
    assert h._extract_json('Sure: {"winner":"tie","reason":"x"} done')["winner"] == "tie"
    assert h._extract_json("no json here") is None
    assert h._extract_json("") is None
    # non-dict JSON must become None (else run_qualitative_judge's .get crashes)
    assert h._extract_json('["A"]') is None
    assert h._extract_json('"A"') is None
    assert h._extract_json("42") is None


def test_judge_pairs_caps_and_handles_empty():
    rng = random.Random(0)
    on = [f"on{i}" for i in range(10)]
    off = [f"off{i}" for i in range(4)]
    pairs = h._judge_pairs(on, off, max_pairs=5, rng=rng)
    assert len(pairs) == 4                 # min(10, 4, 5)
    assert h._judge_pairs([], off, 5, rng) == []
    assert h._judge_pairs(on, [], 5, rng) == []


def test_judge_argv_grants_no_tools():
    # The judge only prints a JSON verdict over attacker-influenced diff text,
    # so it must run with an EMPTY allow-list -- an injection in a diff can't
    # reach a tool. Safety flags (slash-commands off, strict MCP) stay on.
    argv = h._judge_argv("PROMPT-TEXT", _cfg())
    assert "--allowedTools" in argv
    assert argv[argv.index("--allowedTools") + 1] == ""   # empty -> no tools
    joined = " ".join(argv)
    assert "Bash" not in joined and "Write" not in joined  # no tool granted
    # "Edit" appears only inside the permission-mode value "acceptEdits", never
    # as an allowed tool name:
    assert "Edit" not in joined.replace("acceptEdits", "")
    assert "PROMPT-TEXT" in argv                          # prompt still passed through
    assert "--disable-slash-commands" in argv and "--strict-mcp-config" in argv


def _cmp(task, pid, ordering, winner, a="my-skill", b="control",
         pair="my-skill_vs_control"):
    return h.JudgeComparison(task_id=task, pair_id=pid, ordering=ordering,
                             winner_arm=winner, pair=pair, a_label=a, b_label=b)


def test_aggregate_position_bias_washes_out():
    # A judge that ALWAYS picks slot "A": a_first -> a_label wins, b_first -> b_label
    # wins. The pair nets to 1 a + 1 b and reads position-INconsistent.
    comps = [_cmp("t", 0, "a_first", "my-skill"), _cmp("t", 0, "b_first", "control")]
    agg = h.aggregate_judge(comps)["my-skill_vs_control"]
    assert agg["a_wins"] == 1 and agg["b_wins"] == 1
    assert agg["win_rate_a"] == 0.5
    assert agg["total_pairs"] == 1 and agg["consistent"] == 0   # order-sensitive


def test_aggregate_consistent_preference():
    # Both orderings agree a_label is better across two pairs -> win_rate 1.0.
    comps = [_cmp("t", 0, "a_first", "my-skill"), _cmp("t", 0, "b_first", "my-skill"),
             _cmp("t", 1, "a_first", "my-skill"), _cmp("t", 1, "b_first", "my-skill")]
    agg = h.aggregate_judge(comps)["my-skill_vs_control"]
    assert agg["a_wins"] == 4 and agg["b_wins"] == 0 and agg["win_rate_a"] == 1.0
    assert agg["consistent"] == 2 and agg["total_pairs"] == 2


def test_aggregate_judge_is_per_pair():
    # Two comparison pairs in one judge run aggregate independently.
    comps = [_cmp("t", 0, "a_first", "skill-a", a="skill-a", b="skill-b",
                  pair="skill-a_vs_skill-b"),
             _cmp("t", 0, "b_first", "skill-a", a="skill-a", b="skill-b",
                  pair="skill-a_vs_skill-b"),
             _cmp("t", 0, "a_first", "skill-a", a="skill-a", b="control",
                  pair="skill-a_vs_control"),
             _cmp("t", 0, "b_first", "control", a="skill-a", b="control",
                  pair="skill-a_vs_control")]
    agg = h.aggregate_judge(comps)
    assert set(agg) == {"skill-a_vs_skill-b", "skill-a_vs_control"}
    assert agg["skill-a_vs_skill-b"]["win_rate_a"] == 1.0       # A beats B cleanly
    assert agg["skill-a_vs_control"]["win_rate_a"] == 0.5       # position-flipped


def test_build_judge_report_empty_and_basic():
    cfg = _cfg(bootstrap_iters=200)
    assert "NON-DETERMINISTIC" in h.build_judge_report([], cfg)
    comps = [_cmp("a", 0, "a_first", "my-skill"), _cmp("a", 0, "b_first", "my-skill"),
             _cmp("b", 0, "a_first", "my-skill"), _cmp("b", 0, "b_first", "my-skill")]
    rep = h.build_judge_report(comps, cfg)
    assert "win rate: 1.000" in rep and "my-skill" in rep


def test_judge_report_coverage_line():
    cfg = _cfg(bootstrap_iters=100)
    on_with = _rr(Arm.SKILL_ON, True, scores={})
    on_with.diff = "d"
    on_without = _rr(Arm.SKILL_ON, True, scores={})
    on_without.diff = None
    off_with = _rr(Arm.SKILL_OFF, None, scores={})
    off_with.diff = "d"
    rep = h.build_judge_report([], cfg, results=[on_with, on_without, off_with])
    assert "my-skill 1/2" in rep and "control 1/1" in rep


# --------------------------------------------------------------------------
# CLI / config (plan 001)
# --------------------------------------------------------------------------

import tempfile  # noqa: E402


def _tmp(text: str, name: str = "c.toml") -> Path:
    d = Path(tempfile.mkdtemp())
    (d / name).write_text(text)
    return d / name


def test_load_config_round_trips():
    p = _tmp('[experiment]\nrepo_path="/r"\nbase_ref="main"\nskill_src="/s/k"\n'
             'skill_name="k"\nk=3\n[[task]]\nid="t1"\nprompt="do x"\ntest_cmd="pytest"\n')
    cfg, tasks = h.load_config(p)
    assert cfg.skill_name == "k" and cfg.k == 3 and str(cfg.repo_path) == "/r"
    assert len(tasks) == 1 and tasks[0].test_cmd == "pytest"


def test_load_config_rejects_unknown_key():
    p = _tmp('[experiment]\nrepo_path="/r"\nbase_ref="m"\nskill_src="/s"\nskill_name="k"\n'
             'bogus=1\n[[task]]\nid="t"\nprompt="p"\n')
    try:
        h.load_config(p)
        assert False, "should have raised"
    except TypeError:
        pass


# --------------------------------------------------------------------------
# Manifest + summary (plan 002)
# --------------------------------------------------------------------------

def _two_task_results(on_v=1.0, off_v=0.0):
    res = []
    for t in ("A", "B"):
        for arm, v in ((Arm.SKILL_ON, on_v), (Arm.SKILL_OFF, off_v)):
            r = _rr(arm, arm is Arm.SKILL_ON, scores={"tests_pass": v})
            r.task_id = t
            r.diff = f"diff {t} {arm.value}\n+x"
            r.cost_usd = 0.05
            res.append(r)
    return res


def test_manifest_and_summary_shape():
    cfg = _cfg()
    man = h.experiment_manifest(cfg, seed=3, timestamp=1.0)
    assert man["seed"] == 3 and man["timestamp"] == 1.0 and man["harness_version"]
    s = h.summary_dict(_two_task_results(), cfg, man)
    assert s["schema_version"] == 2 and s["primary_metric"] == "tests_pass"
    # convenience mirror of the primary pair still present for verdict/CI consumers
    assert "tests_pass" in s["itt"] and "ci_low" in s["itt"]["tests_pass"]
    assert set(["on_valid", "off_valid", "off_contaminated"]) <= set(s["validity"])
    # 2-arm -> one comparison (skill vs control)
    assert s["primary_pair"] == "my-skill_vs_control"
    assert list(s["comparisons"]) == ["my-skill_vs_control"]
    assert set(s["arms"]) == {"my-skill", "control"}


# --------------------------------------------------------------------------
# Treatment / inputs panel (plan 024 §2): persist + render the independent var
# --------------------------------------------------------------------------

def _skill_cfg(tmp, *, isolation: str, body: str, **kw) -> ExperimentConfig:
    """A cfg whose skill_src is a real on-disk SKILL.md dir (so guidance/sha can be
    reconstructed). `body` is the SKILL.md content."""
    import tempfile as _t
    d = Path(_t.mkdtemp(dir=tmp)) / "my-skill"
    d.mkdir()
    (d / "SKILL.md").write_text(body)
    return _cfg(skill_src=d, skill_name="my-skill", isolation=isolation,
                results_dir=Path(tmp) / "out", k=1, **kw)


def test_treatments_inject_two_arm_shape():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        cfg = _skill_cfg(td, isolation="inject",
                         body="---\nname: my-skill\n---\nWrite a test first.\n")
        tasks = [h.Task(id="t1", prompt="fix the bug")]
        man = h.experiment_manifest(cfg, timestamp=1.0, tasks=tasks)
        tr = man["treatments"]
        assert tr["isolation"] == "inject"
        assert tr["shared_prompt"] == {"t1": "fix the bug"}        # the identical -p arg
        on, off = tr["arms"]["skill_on"], tr["arms"]["skill_off"]
        assert on["role"] == "treatment" and off["role"] == "control"
        # exactly one thing differs: the inject flag (control adds nothing).
        assert on["added"] == ["--append-system-prompt-file", "<injected-guidance>"]
        assert off["added"] == []
        # argv matches reality without leaking a temp path (sentinel stands in).
        assert on["argv"][:3] == ["claude", "-p", "fix the bug"]
        assert "<injected-guidance>" in on["argv"]
        assert "--append-system-prompt-file" not in off["argv"]
        # guidance re-derived purely from the SKILL.md: text + integrity sha present.
        g = on["guidance"]
        assert "Write a test first." in g["text"] and g["truncated"] is False
        assert len(g["sha256"]) == 64
        assert "name: my-skill" not in g["text"]      # frontmatter stripped
        assert "guidance" not in off                  # control injects nothing


def test_treatments_inject_guidance_truncates_at_limit():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        big = "A" * 5000
        cfg = _skill_cfg(td, isolation="inject", body=f"# big\n{big}\n",
                         judge_max_diff_chars=200)
        man = h.experiment_manifest(cfg, timestamp=1.0,
                                    tasks=[h.Task(id="t1", prompt="p")])
        g = man["treatments"]["arms"]["skill_on"]["guidance"]
        assert g["truncated"] is True and len(g["text"]) == 200
        assert len(g["sha256"]) == 64                 # sha of the FULL guidance


def test_treatments_worktree_records_installed_skill():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        cfg = _skill_cfg(td, isolation="worktree", body="# guide\nstuff\n")
        man = h.experiment_manifest(cfg, timestamp=1.0,
                                    tasks=[h.Task(id="t1", prompt="p")])
        on, off = man["treatments"]["arms"]["skill_on"], man["treatments"]["arms"]["skill_off"]
        assert on["added"] == []                      # no argv difference in worktree mode
        inst = on["installed_skill"]
        assert inst["name"] == "my-skill" and inst["path"] == ".claude/skills/my-skill/"
        assert len(inst["sha256"]) == 64
        assert "guidance" not in on                   # worktree loads at runtime, not injected
        assert "installed_skill" not in off           # control installs nothing


def test_treatments_offline_keeps_prompt_skips_fs():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        cfg = _skill_cfg(td, isolation="inject", body="# g\nbody\n")
        man = h.experiment_manifest(cfg, timestamp=1.0, offline=True,
                                    tasks=[h.Task(id="t1", prompt="p")])
        tr = man["treatments"]
        assert tr["shared_prompt"] == {"t1": "p"}      # pure -> still emitted
        on = tr["arms"]["skill_on"]
        assert on["added"] == ["--append-system-prompt-file", "<injected-guidance>"]
        assert "guidance" not in on                    # FS read skipped offline


def test_build_agent_argv_parity_with_run_agent():
    """run_agent must build its argv THROUGH build_agent_argv (no drift), and the
    sentinel inject path must match the real one except for the file token."""
    import asyncio
    cfg = _cfg()
    task = h.Task(id="t1", prompt="do it")
    captured = {}

    async def _fake_spawn(argv, cwd, timeout_s, stdin_data=None):
        captured["argv"] = list(argv)
        return (0, b'{"type":"result","is_error":false,"num_turns":1}\n', b"", False)

    orig = h._spawn_and_drain
    h._spawn_and_drain = _fake_spawn
    try:
        asyncio.run(h.run_agent(Path("/wt"), task, cfg,
                                inject_file=Path("/tmp/realinject.md"),
                                disable_skills=True, model="claude-x"))
    finally:
        h._spawn_and_drain = orig
    expect = h.build_agent_argv(task, cfg, inject_file=Path("/tmp/realinject.md"),
                                disable_skills=True, model="claude-x")
    assert captured["argv"] == expect            # run_agent really uses the extracted argv

    real = h.build_agent_argv(task, cfg, inject_file=Path("/tmp/realinject.md"),
                              disable_skills=True)
    sent = h.build_agent_argv(task, cfg, inject_file="<injected-guidance>",
                              disable_skills=True)
    assert real[:-1] == sent[:-1]                 # identical except the file token
    assert real[-1] == "/tmp/realinject.md" and sent[-1] == "<injected-guidance>"


def test_treatment_panel_renders_and_escapes_hostile_inputs():
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        cfg = _skill_cfg(
            td, isolation="inject",
            body="---\nname: my-skill\n---\nGUIDE_TKN <script>alert('g')</script>\n")
        hostile = 'PROMPT_TKN "><img src=x onerror=alert(1)>'
        tasks = [h.Task(id="t1", prompt=hostile)]
        man = h.experiment_manifest(cfg, timestamp=1.0, tasks=tasks)
        res = [_rr(Arm.SKILL_ON, True, arm_skill_name="my-skill"),
               _rr(Arm.SKILL_OFF, None)]
        doc = h.build_html_report(res, h.Preflight(), cfg, man)
        # panel present with the guided "one thing differed" framing
        assert "Treatment" in doc and "exactly one thing" in doc
        assert "PROMPT_TKN" in doc and "--append-system-prompt-file" in doc
        assert "baseline &mdash; nothing added" in doc     # control row
        # hostile bytes are escaped, never raw
        assert "<img src=x onerror" not in doc
        assert "<script>alert('g')</script>" not in doc
        assert "&lt;script&gt;" in doc
        # the load-bearing claim: guidance + prompt NEVER reach window.SKILLS_TEST
        blob = _skills_test_blob(doc)
        blob_text = json.dumps(blob)
        assert "GUIDE_TKN" not in blob_text and "PROMPT_TKN" not in blob_text
        assert "treatments" not in blob


def test_gallery_renders_two_summaries():
    # SPIKE (plan 020): a static index over many self-reported summary.json files.
    man = h.experiment_manifest(_cfg(), timestamp=1.0, offline=True)
    sx = h.summary_dict(_two_task_results(), _cfg(skill_name="skill-x"), man)
    sy = h.summary_dict(_two_task_results(), _cfg(skill_name="skill-y"), man)
    out = h.build_gallery_html([{"summary": sx, "report_href": "x/report.html"},
                                {"summary": sy, "report_href": None}])
    assert "skill-x" in out and "skill-y" in out
    # gallery cards show a clean comparison chip (delta-chip), no badge artifact
    assert "self-reported" in out and "delta-chip" in out
    assert "open report" in out          # the linked entry surfaces its report link


def test_gallery_skips_unsupported_schema():
    out = h.build_gallery_html([{"summary": {"schema_version": 1}}])
    assert "unsupported schema" in out.lower()


def test_gallery_verdict_none_without_estimate():
    # v2 summary whose primary metric has no itt entry -> verdict None, no crash.
    s = {"schema_version": 2, "primary_metric": "tests_pass", "itt": {},
         "manifest": {"skill_name": "s"}, "primary_pair": "s_vs_control"}
    assert h._gallery_verdict_from_summary(s) is None
    out = h.build_gallery_html([{"summary": s, "report_href": None}])
    assert "</html>" in out and "s_vs_control" in out


def _three_arm_results():
    """skill A clearly > control; skill B between; so A beats control AND A beats B."""
    res = []
    table = {Arm.SKILL_ON: 1.0, Arm.SKILL_B: 0.5, Arm.SKILL_OFF: 0.0}
    names = {Arm.SKILL_ON: "skill-a", Arm.SKILL_B: "skill-b", Arm.SKILL_OFF: None}
    for t in ("A", "B"):
        for arm, v in table.items():
            for i in range(3):
                r = RunResult(task_id=t, arm=arm, run_index=i, worktree=Path("/wt"),
                              skill_activated=(True if names[arm] else None),
                              activation_reason="", agent_ok=True, completed=True,
                              cost_usd=0.05, arm_skill_name=names[arm],
                              scores={"tests_pass": v, "lint_pass": 1.0})
                res.append(r)
    return res


def test_head_to_head_three_arm_summary():
    cfg = _cfg(skill_name="skill-a", skill_b_src=Path("/s/b"), skill_b_name="skill-b")
    assert h.experiment_arms(cfg) == [Arm.SKILL_ON, Arm.SKILL_B, Arm.SKILL_OFF]
    assert h.primary_pair(cfg) == (Arm.SKILL_ON, Arm.SKILL_B)   # headline = A vs B
    man = h.experiment_manifest(cfg, timestamp=1.0, offline=True)
    s = h.summary_dict(_three_arm_results(), cfg, man)
    assert set(s["arms"]) == {"skill-a", "skill-b", "control"}
    # all three pairwise comparisons present
    assert set(s["comparisons"]) == {"skill-a_vs_control", "skill-b_vs_control",
                                     "skill-a_vs_skill-b"}
    assert s["primary_pair"] == "skill-a_vs_skill-b"
    # A beats control on tests_pass (delta +1.0)
    assert s["comparisons"]["skill-a_vs_control"]["itt"]["tests_pass"]["point"] == 1.0
    # A beats B (delta +0.5)
    assert s["comparisons"]["skill-a_vs_skill-b"]["itt"]["tests_pass"]["point"] == 0.5


def test_cross_runner_report_downgrades_and_banners():
    # claude (skill A) vs codex (runner_b) -> the primary pair is cross-runner, so the
    # report's verdict is forced to 'suggestive' and a confound banner is rendered,
    # EVEN THOUGH the underlying delta is a clean +0.5 that would otherwise read green.
    cfg = _cfg(skill_name="skill-a", runner_b="codex")
    assert h.primary_pair(cfg) == (Arm.SKILL_ON, Arm.SKILL_B)
    assert h._pair_is_cross_runner(cfg, Arm.SKILL_ON, Arm.SKILL_B)
    man = h.experiment_manifest(cfg, timestamp=1.0, offline=True)
    doc = h.build_html_report(_three_arm_results(), h.Preflight(), cfg, man)
    data = _skills_test_blob(doc)
    assert data["verdict"]["label"] == "suggestive"
    assert data["verdict"]["crossRunner"] is True
    assert data["meta"]["crossRunner"] is True
    assert data["meta"]["armRunners"]["codex"] == "codex"
    assert data["meta"]["armRunners"]["skill-a"] == "claude"
    # the confound note + the CLI-agnostic judge pointer are in the rendered HTML
    assert "Confounded" in doc and "different agent CLIs" in doc
    # manifest persists the per-arm runner map for downstream consumers
    assert man["arm_runners"]["codex"] == "codex"


# --------------------------------------------------------------------------
# Comparison verdict (significant / inconclusive + direction)
# --------------------------------------------------------------------------

def test_comparison_verdict_self_polices():
    base = {"point": 0.18, "ci_low": 0.06, "ci_high": 0.30}
    # CI clears 0 + trustworthy -> significant; direction favours the left/treatment arm.
    v = h.comparison_verdict(base, 1, 3, True, 0)
    assert v["label"] == "significant" and v["direction"] == 1 and v["tone"] == "accent"
    # CI straddles 0 -> inconclusive (flat tone).
    incon = h.comparison_verdict({"point": 0.1, "ci_low": -0.05, "ci_high": 0.25},
                                 1, 3, True, 0)
    assert incon["label"] == "inconclusive" and incon["tone"] == "flat"
    assert h.comparison_verdict(base, 1, 1, True, 0)["label"] == "inconclusive"   # <2 tasks
    assert h.comparison_verdict(base, 1, 3, False, 0)["label"] == "inconclusive"  # unclustered
    assert h.comparison_verdict(base, 1, 3, True, 2)["label"] == "inconclusive"   # contaminated
    # A significant effect AGAINST the treatment arm is still 'significant', dir -1
    # (the winner/direction is in the headline, not a 'regressed' verdict).
    reg = {"point": -0.2, "ci_low": -0.3, "ci_high": -0.05}
    rv = h.comparison_verdict(reg, 1, 3, True, 0)
    assert rv["label"] == "significant" and rv["direction"] == -1
    # Metric direction flips the sense of 'favours left': lower-is-better + a negative
    # point favours the left arm (direction +1).
    lower = h.comparison_verdict(reg, -1, 3, True, 0)
    assert lower["label"] == "significant" and lower["direction"] == 1


def test_no_badge_artifact_functions():
    # The shields badge artifact is retired; only the comparison verdict survives.
    for gone in ("render_badge_svg", "badge_endpoint_json", "badge_markdown",
                 "badge_verdict"):
        assert not hasattr(h, gone), f"{gone} should have been removed"


# --------------------------------------------------------------------------
# HTML report + from_dict (plan 004)
# --------------------------------------------------------------------------

def test_runresult_from_dict_round_trips():
    r = _rr(Arm.SKILL_ON, True, scores={"tests_pass": 1.0})
    r.diff = "diff --git a b\n+x"
    r2 = h.RunResult.from_dict(r.to_dict())
    assert r2.task_id == r.task_id and r2.arm is r.arm
    assert r2.scores == r.scores and r2.diff == r.diff and r2.itt_valid == r.itt_valid


def test_dedupe_runs_collapses_retried_cells():
    def cell(arm, ok, idx):           # _rr hardcodes run_index=0; set it after
        r = _rr(arm, True if ok else None, agent_ok=ok)
        r.run_index = idx
        return r
    # failed-then-succeeded: the success wins, one row out.
    failed = cell(Arm.SKILL_ON, False, 0)
    retry_ok = cell(Arm.SKILL_ON, True, 0)
    out = h._dedupe_runs([failed], [retry_ok])
    assert len(out) == 1 and out[0].agent_ok is True
    # failed-then-failed: still exactly one row.
    assert len(h._dedupe_runs([cell(Arm.SKILL_ON, False, 1)],
                              [cell(Arm.SKILL_ON, False, 1)])) == 1
    # an untouched successful cell (different key, only in prior) passes through.
    done_ok = cell(Arm.SKILL_OFF, True, 2)
    out2 = h._dedupe_runs([done_ok, failed], [retry_ok])
    assert len(out2) == 2
    keys = [(r.task_id, r.arm.value, r.run_index) for r in out2]
    # stable order: prior cells keep their first-seen position.
    assert keys == [("t", Arm.SKILL_OFF.value, 2), ("t", Arm.SKILL_ON.value, 0)]


def test_build_html_report_renders_and_escapes():
    cfg = _cfg()
    res = _two_task_results()
    # A real `git diff` whose CONTENT carries markup -- the parsed renderer must
    # escape it per segment so it can never re-parse as live HTML.
    res[0].diff = ("diff --git a/x.py b/x.py\n--- a/x.py\n+++ b/x.py\n"
                   "@@ -1,1 +1,1 @@\n-old = 1\n+evil = '<script>alert(1)</script>'\n")
    man = h.experiment_manifest(cfg, timestamp=1.0)
    doc = h.build_html_report(res, h.Preflight(), cfg, man)
    assert doc.lstrip().startswith("<") and "</html>" in doc and "<table" in doc
    assert "&lt;script&gt;" in doc and "<script>alert(1)" not in doc


def test_tooltip_payload_is_double_escaped_against_attr_roundtrip():
    # Arm labels come from skill_name / CLI args / --from-github config and are
    # rendered into data-tip="..." then read back via getAttribute (one decode)
    # into innerHTML (a second decode). The tooltip builder must escape dynamic
    # text TWICE so a markup label can't re-parse as live HTML (XSS).
    cfg = _cfg(skill_name="<img src=x onerror=alert(1)>")
    res = _two_task_results()
    man = h.experiment_manifest(cfg, timestamp=1.0)
    doc = h.build_html_report(res, h.Preflight(), cfg, man)  # must not raise
    assert doc.lstrip().startswith("<") and "</html>" in doc
    assert "function escTip(s){ return esc(esc(s)); }" in doc
    assert "escTip(head)" in doc
    assert "escTip(r[0])" in doc and "escTip(r[1])" in doc
    # the pre-fix vulnerable shapes must not survive:
    assert '"<div class=\'tt-h\'>" + esc(head) + "</div>"' not in doc
    assert '" <b>" + r[1] + "</b>"' not in doc


def test_inconclusive_verdict_does_not_assert_settled_headline():
    # 1 task, ON tests_pass=1.0 vs OFF=0.0 -> primary CI [1.0,1.0] (clears 0),
    # but n_tasks<2 so comparison_verdict -> "inconclusive". The hero headline must
    # NOT read as a settled claim when the pill says inconclusive.
    cfg = _cfg()
    res = []
    for arm, v in ((Arm.SKILL_ON, 1.0), (Arm.SKILL_OFF, 0.0)):
        for i in range(3):
            r = _rr(arm, arm is Arm.SKILL_ON, scores={"tests_pass": v})
            r.task_id = "only"
            r.run_index = i
            r.cost_usd = 0.05
            res.append(r)
    man = h.experiment_manifest(cfg, timestamp=1.0, offline=True)
    doc = h.build_html_report(res, h.Preflight(), cfg, man)
    data = _skills_test_blob(doc)
    assert data["verdict"]["label"] == "inconclusive"
    # primary comparison CI clears 0 -> the bug condition is actually present
    prim = next(c for c in data["comparisons"] if c["key"] == data["meta"]["primaryPair"])
    assert prim["itt"]["tests_pass"]["lo"] > 0, "setup must make the primary CI exclude 0"
    assert "No significant difference" in doc       # honest pill text intact
    assert "Suggestive only" in doc                  # the fix shipped


def test_build_html_report_survives_all_ties_judge_pair():
    # An all-ties pair makes win_rate_a NaN -> JSON null; the embedded JS used to
    # crash on null.toFixed(), blanking the whole #app. The doc must still carry
    # the page shell, the null precondition, and the guard token.
    cfg = _cfg()
    res = _two_task_results()
    man = h.experiment_manifest(cfg, timestamp=1.0)
    comps = [_cmp("A", 0, "a_first", "tie"), _cmp("A", 0, "b_first", "tie")]
    doc = h.build_html_report(res, h.Preflight(), cfg, man, comparisons=comps)
    assert doc.lstrip().startswith("<") and "</html>" in doc
    assert '"win_rate_a": null' in doc            # the JS-crash precondition
    assert "wrStr" in doc                          # the guard landed


def _skills_test_blob(doc: str) -> dict:
    m = re.search(r"window\.SKILLS_TEST=(.+?);\n\(function\(\)\{", doc, re.S)
    assert m, "window.SKILLS_TEST blob not found / injection boundary changed"
    return json.loads(m.group(1))   # already valid JSON (\/ escape is legal)


def test_html_blob_shape_two_arm():
    cfg = _cfg()
    man = h.experiment_manifest(cfg, timestamp=1.0)
    data = _skills_test_blob(h.build_html_report(_two_task_results(), h.Preflight(), cfg, man))
    assert set(data) >= {"metrics", "primary", "dir", "arms", "armColors", "tasks",
                         "taskColors", "runs", "armMeans", "comparisons", "judge",
                         "verdict", "meta"}
    assert data["primary"] == "tests_pass"
    assert set(data["arms"]) == {"my-skill", "control"}
    assert set(data["armColors"]) == {"my-skill", "control"}
    assert data["meta"]["estimand"] == "ITT" and data["meta"]["primaryPair"]
    assert data["meta"]["control"] == "control"
    row = data["runs"][0]
    assert set(row) >= {"task", "arm", "idx", "valid", "cost", "turns",
                        "activated", "contam", "scores"}
    assert "tests_pass" in data["armMeans"]["my-skill"]
    assert len(data["comparisons"]) == 1
    c = data["comparisons"][0]
    assert {"key", "a", "b", "itt"} <= set(c)
    d = c["itt"]["tests_pass"]
    assert set(d) == {"point", "lo", "hi", "p", "q"}   # lo/hi, NOT ci_low/ci_high
    assert data["judge"] == []


def test_html_blob_shape_three_arm():
    cfg = _cfg(skill_name="skill-a", skill_b_src=Path("/s/b"), skill_b_name="skill-b")
    man = h.experiment_manifest(cfg, timestamp=1.0, offline=True)
    data = _skills_test_blob(
        h.build_html_report(_three_arm_results(), h.Preflight(), cfg, man))
    assert set(data["arms"]) == {"skill-a", "skill-b", "control"}
    keys = {c["key"] for c in data["comparisons"]}
    assert keys == {"skill-a_vs_control", "skill-b_vs_control", "skill-a_vs_skill-b"}
    assert data["meta"]["primaryPair"] == "skill-a_vs_skill-b"
    for c in data["comparisons"]:
        assert "tests_pass" in c["itt"]


def test_html_blob_judge_subblob_and_all_ties():
    cfg = _cfg()
    man = h.experiment_manifest(cfg, timestamp=1.0)
    comps = [
        _cmp("t", 0, "a_first", "my-skill"), _cmp("t", 0, "b_first", "my-skill"),
        _cmp("t", 1, "a_first", "tie"), _cmp("t", 1, "b_first", "tie"),
    ]
    data = _skills_test_blob(
        h.build_html_report(_two_task_results(), h.Preflight(), cfg, man, comparisons=comps))
    j = data["judge"]
    assert len(j) == 1                       # one pair key: my-skill_vs_control
    e = j[0]
    assert {"pair", "a", "b", "a_wins", "b_wins", "ties", "win_rate_a",
            "consistency"} <= set(e)
    assert e["a_wins"] == 2 and e["ties"] == 2
    assert e["win_rate_a"] == 1.0            # decisive verdicts all favor A


def test_html_blob_judge_win_rate_null_when_no_decisive():
    cfg = _cfg()
    man = h.experiment_manifest(cfg, timestamp=1.0)
    comps = [_cmp("t", 0, "a_first", "tie"), _cmp("t", 0, "b_first", "tie")]
    data = _skills_test_blob(
        h.build_html_report(_two_task_results(), h.Preflight(), cfg, man, comparisons=comps))
    assert data["judge"][0]["win_rate_a"] is None   # nan -> JSON null


def test_verdict_blob_maps_each_label():
    # Comparison framing: significant -> accent "Significant difference"; inconclusive
    # -> flat "No significant difference". No pass/fail "verified"/"regressed" text.
    sig = h._verdict_blob({"label": "significant", "direction": 1})
    assert sig == {"label": "significant", "tone": "accent",
                   "text": "Significant difference", "crossRunner": False}
    incon = h._verdict_blob({"label": "inconclusive", "direction": -1})
    assert incon == {"label": "inconclusive", "tone": "flat",
                     "text": "No significant difference", "crossRunner": False}
    assert h._verdict_blob(None)["label"] == "inconclusive"   # no KeyError


def test_verdict_blob_cross_runner_downgrades_to_suggestive():
    # A cross-runner primary pair can never read accent -- it's confounded.
    for label, direction in (("significant", 1), ("significant", -1),
                             ("inconclusive", 1)):
        b = h._verdict_blob({"label": label, "direction": direction}, cross_runner=True)
        assert b["label"] == "suggestive"
        assert b["tone"] == "flat"          # grey, never accent
        assert b["crossRunner"] is True
        assert "Suggestive" in b["text"]
    # a SIGNIFICANT cross-runner gap still leaks its direction as a hint, not a verdict
    assert "second CLI" in h._verdict_blob(
        {"label": "significant", "direction": 1}, cross_runner=True)["text"]
    assert "first CLI" in h._verdict_blob(
        {"label": "significant", "direction": -1}, cross_runner=True)["text"]
    # an inconclusive cross-runner pair adds no directional hint
    assert h._verdict_blob({"label": "inconclusive", "direction": 1},
                           cross_runner=True)["text"].endswith("skill/model change")


def test_html_script_structural_and_optional_node_check():
    cfg = _cfg()
    man = h.experiment_manifest(cfg, timestamp=1.0)
    doc = h.build_html_report(_two_task_results(), h.Preflight(), cfg, man)
    for fn in ("function leadSignal(", "function barChart(", "function forestChart(",
               "function stripChart(", "function judgeSection("):
        assert fn in doc, f"missing JS function: {fn}"
    m = re.search(r"<script>(.+?)</script>", doc, re.S)
    assert m, "script tag not found"
    script = m.group(1)
    node = shutil.which("node")
    if not node:
        print("    (node not on PATH; skipping node --check)")
        return
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(script)
        path = f.name
    proc = subprocess.run([node, "--check", path], capture_output=True, text=True)
    assert proc.returncode == 0, f"node --check failed:\n{proc.stderr}"


# --------------------------------------------------------------------------
# Parsed diff renderer (plan 024 §1.1-§1.8)
# --------------------------------------------------------------------------

_MULTI_DIFF = (
    "diff --git a/src/app.py b/src/app.py\n"
    "index 1111111..2222222 100644\n"
    "--- a/src/app.py\n"
    "+++ b/src/app.py\n"
    "@@ -10,6 +10,7 @@ def main():\n"
    " ctx-a\n"
    "-old line\n"
    "+new line\n"
    "+extra add\n"
    " ctx-b\n"
    "@@ -40,3 +41,3 @@\n"
    " ctx-c\n"
    "-gone\n"
    "+added\n"
    "diff --git a/NOTES.md b/NOTES.md\n"
    "new file mode 100644\n"
    "--- /dev/null\n"
    "+++ b/NOTES.md\n"
    "@@ -0,0 +1,2 @@\n"
    "+# Notes\n"
    "+second\n"
    "diff --git a/dead.py b/dead.py\n"
    "deleted file mode 100644\n"
    "--- a/dead.py\n"
    "+++ /dev/null\n"
    "@@ -1,2 +0,0 @@\n"
    "-bye\n"
    "-now\n"
)


def test_parse_unified_diff_line_number_seeding():
    files = h.parse_unified_diff(_MULTI_DIFF)
    assert [f.path for f in files] == ["src/app.py", "NOTES.md", "dead.py"]
    # status classified from metadata, NOT from a ctx fall-through
    assert [f.status for f in files] == ["M", "A", "D"]
    # +adds/-dels counted per file
    assert (files[0].add_count, files[0].del_count) == (3, 2)
    assert (files[1].add_count, files[1].del_count) == (2, 0)
    assert (files[2].add_count, files[2].del_count) == (0, 2)
    # line numbers seeded from each @@ and advanced (old# on ctx/del, new# on ctx/add)
    h0 = files[0].hunks[0]
    assert [(r.kind, r.old_n, r.new_n) for r in h0.rows] == [
        ("ctx", 10, 10), ("del", 11, None), ("add", None, 11),
        ("add", None, 12), ("ctx", 12, 13)]
    h1 = files[0].hunks[1]               # second hunk re-seeds from its own header
    assert [(r.kind, r.old_n, r.new_n) for r in h1.rows] == [
        ("ctx", 40, 41), ("del", 41, None), ("add", None, 42)]
    # the deleted file's rows carry old# only
    assert [(r.kind, r.old_n, r.new_n) for r in files[2].hunks[0].rows] == [
        ("del", 1, None), ("del", 2, None)]
    # +++/---/index lines are metadata, never emitted as rows
    assert all(r.kind in ("ctx", "add", "del") for f in files for hk in f.hunks
               for r in hk.rows)


def test_word_diff_per_segment_escaping_inside_w_span():
    # A renamed identifier whose NEW token contains '<' must surface as &lt; INSIDE
    # a .w span -- proving each get_opcodes() segment is escaped before wrapping.
    diff = ("diff --git a/m.py b/m.py\n--- a/m.py\n+++ b/m.py\n@@ -1,1 +1,1 @@\n"
            "-def greet(name):\n+def greet(foo<baz>):\n")
    files = h.parse_unified_diff(diff)
    hunk = h.word_diff_pairs(files[0].hunks[0])
    drow = next(r for r in hunk.rows if r.kind == "del")
    arow = next(r for r in hunk.rows if r.kind == "add")
    assert drow.pair_idx == 0 and arow.pair_idx == 0     # paired by index
    assert any(changed for _, changed in arow.segments)  # a changed segment exists
    out = h.render_diff(diff, "m")
    assert '<span class="w">' in out                     # word highlight ran
    assert "&lt;" in out and "foo<baz>" not in out        # raw byte never leaks
    assert re.search(r'<span class="w">[^<]*&lt;', out)   # the < lives inside a .w span


def test_word_diff_ratio_gate_and_length_cap_fall_back_to_whole_line():
    # ratio < 0.3 -> no char diff (the two lines share nothing)
    low = ("diff --git a/g b/g\n@@ -1,1 +1,1 @@\n-aaaaaaaaaa\n+zzzz1234567\n")
    drow = h.word_diff_pairs(h.parse_unified_diff(low)[0].hunks[0]).rows[0]
    assert drow.segments == [("aaaaaaaaaa", False)]      # untouched whole line
    assert '<span class="w">' not in h.render_diff(low, "g")
    # over the 400-char cap -> no char diff even though the lines are near-identical
    base = "x" * 410
    capped = f"diff --git a/c b/c\n@@ -1,1 +1,1 @@\n-{base}A\n+{base}B\n"
    assert '<span class="w">' not in h.render_diff(capped, "c")
    # within the cap and above the gate -> char diff DOES fire
    near = "y" * 100
    ok = f"diff --git a/c2 b/c2\n@@ -1,1 +1,1 @@\n-{near}A\n+{near}B\n"
    assert '<span class="w">' in h.render_diff(ok, "c2")


def test_fold_context_collapses_long_runs_keeping_lead_and_trail():
    rows = ([h._Row("ctx", i, i, [(f"c{i}", False)]) for i in range(10)]
            + [h._Row("add", None, 11, [("x", False)])])
    folded = h.fold_context(rows, ctx=3, min_run=6)
    folds = [r for r in folded if r.kind == "fold"]
    assert len(folds) == 1 and folds[0].fold_n == 4          # 10 ctx - 3 lead - 3 trail
    assert len(folds[0].fold_rows) == 4
    # short runs are left intact
    short = [h._Row("ctx", i, i, [("c", False)]) for i in range(5)]
    assert all(r.kind == "ctx" for r in h.fold_context(short))


def test_render_diff_structure_and_fold_marker():
    diff = ("diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
            "@@ -1,14 +1,14 @@\n"
            + "".join(f" ctx{i}\n" for i in range(10))   # 10 ctx -> fold marker
            + "-old\n+new\n")
    out = h.render_diff(diff, "s")
    for needle in ('class="file-head"', 'class="fpath"', 'class="fcount"',
                   'class="hunk-head"', 'class="row ctx"', 'class="row add"',
                   'class="row del"', 'class="ln"', 'class="sg"', 'class="tx"',
                   'data-k="0"', 'data-k="1"', 'data-k="2"', 'data-status="M"',
                   'class="fold"'):
        assert needle in out, needle


def test_render_diff_truncation_marker_and_field_roundtrip():
    diff = "diff --git a/x b/x\n@@ -1,1 +1,1 @@\n-a\n+b\n"
    assert 'class="trunc"' in h.render_diff(diff, "x", True)
    assert 'class="trunc"' not in h.render_diff(diff, "x", False)
    # the diff_truncated flag threads through to_dict/from_dict
    r = RunResult(task_id="t", arm=Arm.SKILL_ON, run_index=0, worktree=Path("/wt"),
                  skill_activated=None, activation_reason="", agent_ok=True,
                  diff="d", diff_truncated=True)
    assert r.to_dict()["diff_truncated"] is True
    assert RunResult.from_dict(r.to_dict()).diff_truncated is True


# --------------------------------------------------------------------------
# Interactive .cmp diff shell + DiffViewer (plan 024 §1.2/§1.3/§1.5, batch 2)
# --------------------------------------------------------------------------

def _results_with_diff(diff_on: str, diff_off: str = "") -> list:
    """Two-task results carrying real git diffs on the ON arm (so the parsed renderer
    + work blob have files to chew on)."""
    res = _two_task_results()
    for r in res:
        r.diff = diff_on if r.arm is Arm.SKILL_ON else (diff_off or diff_on)
    return res


def test_work_products_renders_cmp_shell_per_task():
    cfg = _cfg()
    man = h.experiment_manifest(cfg, timestamp=1.0)
    diff = ("diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
            "@@ -1,1 +1,1 @@\n-old\n+new\n")
    doc = h.build_html_report(_results_with_diff(diff), h.Preflight(), cfg, man)
    # one .cmp shell per task (A, B), each with the toolbar + panes scaffold
    assert doc.count('class="cmp"') == 2
    for needle in ('class="cmp-bar"', 'class="armrun"', 'class="runsel"',
                   'class="modesw"', 'data-mode="compare"', 'class="viewsw"',
                   'data-act="split"', 'wrapbtn', 'class="diffsearch"',
                   'class="filerail"', 'class="panes"', 'class="pane"',
                   'data-arm="skill_on"', 'data-arm="skill_off"',
                   'class="diffview"'):
        assert needle in doc, needle
    # the diff body is still rendered + escaped per the batch-1 engine
    assert 'class="row add"' in doc and 'class="tx"' in doc


def test_work_blob_carries_only_ids_counts_flags_never_diff_text():
    # LOAD-BEARING (plan 024 §1.4): the JSON blob must carry numbers/ids/flags only.
    # Plant a unique token in BOTH the diff code and the file path; it must be ABSENT
    # from window.SKILLS_TEST yet present (escaped) in the server-rendered DOM.
    cfg = _cfg()
    man = h.experiment_manifest(cfg, timestamp=1.0)
    token = "ZQX_PLANTED_SECRET_4242"
    diff = (f"diff --git a/{token}_path.py b/{token}_path.py\n"
            f"--- a/{token}_path.py\n+++ b/{token}_path.py\n"
            f"@@ -1,1 +1,1 @@\n-was = '{token}_old'\n+now = '{token}_new'\n")
    doc = h.build_html_report(_results_with_diff(diff), h.Preflight(), cfg, man)
    blob_text = re.search(r"window\.SKILLS_TEST=(.+?);\n\(function\(\)\{",
                          doc, re.S).group(1)
    assert token not in blob_text, "diff text/path leaked into window.SKILLS_TEST"
    assert token in doc, "diff token should still appear (escaped) in the DOM"
    data = json.loads(blob_text)
    work = data["work"]
    assert set(work) == {"A", "B"}
    t = work["A"]
    assert set(t) == {"files", "arms", "truncated"}
    # files: only id/add/del/status keys, status a single letter, counts ints
    f0 = t["files"][0]
    assert set(f0) == {"id", "add", "del", "status"}
    assert f0["id"].startswith("f-") and isinstance(f0["add"], int)
    assert f0["status"] in ("A", "M", "D", "R")
    # the blob's file id must address a real node in the rendered DOM
    assert f'id="{f0["id"]}"' in doc
    # arms keyed by arm.value, each with run_ids + a representative id
    assert set(t["arms"]) == {"skill_on", "skill_off"}
    on = t["arms"]["skill_on"]
    assert set(on) == {"run_ids", "rep"} and on["rep"] == on["run_ids"][0]
    # truncated is a flag map keyed by run id
    assert all(isinstance(v, bool) for v in t["truncated"].values())


def test_work_blob_honours_truncation_flag():
    cfg = _cfg()
    man = h.experiment_manifest(cfg, timestamp=1.0)
    diff = "diff --git a/x.py b/x.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
    res = _results_with_diff(diff)
    for r in res:                      # mark the ON arm's diffs truncated
        if r.arm is Arm.SKILL_ON:
            r.diff_truncated = True
    data = _skills_test_blob(h.build_html_report(res, h.Preflight(), cfg, man))
    on_rep = data["work"]["A"]["arms"]["skill_on"]["rep"]
    off_rep = data["work"]["A"]["arms"]["skill_off"]["rep"]
    assert data["work"]["A"]["truncated"][on_rep] is True
    assert data["work"]["A"]["truncated"][off_rep] is False


def test_diffviewer_iife_is_wired_and_escape_safe():
    # The interaction layer must be invoked beside the existing wiring and must use
    # the escape-safe primitives (clone/Range/textContent), NEVER innerHTML of diff.
    cfg = _cfg()
    man = h.experiment_manifest(cfg, timestamp=1.0)
    doc = h.build_html_report(_two_task_results(), h.Preflight(), cfg, man)
    assert "function DiffViewer(" in doc and "\n  DiffViewer();\n" in doc
    script = re.search(r"<script>(.+?)</script>", doc, re.S).group(1)
    diffviewer = script[script.index("function DiffViewer("):]
    # escape-safe primitives present
    for needle in ("cloneNode(true)", "surroundContents", ".textContent",
                   "execCommand", "navigator.clipboard", "localStorage",
                   "IntersectionObserver", "requestAnimationFrame",
                   "createTreeWalker"):
        assert needle in diffviewer, needle
    # the DiffViewer body must never ASSIGN innerHTML (would defeat per-segment escaping)
    assert re.search(r"innerHTML\s*=", diffviewer) is None


def test_minimap_canvas_per_pane_and_painted_from_data_k_ints():
    # Change-density minimap (graft: Blink): one <canvas.minimap> per pane, painted
    # client-side from data-k ints + computed colors -- never diff text, never
    # innerHTML (escape-safe by construction).
    cfg = _cfg()
    man = h.experiment_manifest(cfg, timestamp=1.0)
    diff = ("diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n"
            "@@ -1,1 +1,1 @@\n-old\n+new\n")
    doc = h.build_html_report(_results_with_diff(diff), h.Preflight(), cfg, man)
    # exactly one minimap canvas per server-rendered pane
    assert doc.count('class="minimap"') == doc.count('class="pane"')
    assert doc.count('class="minimap"') > 0
    script = re.search(r"<script>(.+?)</script>", doc, re.S).group(1)
    diffviewer = script[script.index("function DiffViewer("):]
    # the paint path reads data-k INTEGERS (parseInt), paints a <canvas> 2d context,
    # tracks a viewport rect on scroll, and scrubs scrollTop by pointer drag
    for needle in ("function mmPaint(", '.querySelector(".minimap")', 'getContext("2d")',
                   'parseInt(rows[j].getAttribute("data-k")', "pointerdown",
                   "pane.scrollTop =", "getComputedStyle"):
        assert needle in diffviewer, needle
    # still no innerHTML assignment anywhere in the body (minimap stays escape-safe)
    assert re.search(r"innerHTML\s*=", diffviewer) is None


def test_three_arm_report_renders_a_pane_per_arm():
    cfg = _cfg(skill_name="skill-a", skill_b_src=Path("/s/b"), skill_b_name="skill-b")
    man = h.experiment_manifest(cfg, timestamp=1.0, offline=True)
    doc = h.build_html_report(_three_arm_results(), h.Preflight(), cfg, man)
    # arm-symmetric: a pane (and run picker) for each of the three arms
    for av in ("skill_on", "skill_b", "skill_off"):
        assert f'data-arm="{av}"' in doc
    data = _skills_test_blob(doc)
    some_task = next(iter(data["work"].values()))
    assert set(some_task["arms"]) == {"skill_on", "skill_b", "skill_off"}


# --------------------------------------------------------------------------
# Contrast band: scorecards + divergence map + beat captions (plan 024 §1.6)
# --------------------------------------------------------------------------

def test_divergence_classifies_both_skill_only_ctrl_only():
    # set-compare the primary pair's touched paths: A=treatment ('skill'), B=reference.
    cls = h._divergence({"a.py", "shared.py"}, {"shared.py", "b.py"})
    assert cls["shared.py"] == "both"
    assert cls["a.py"] == "skill-only"          # only the treatment arm A touched it
    assert cls["b.py"] == "ctrl-only"           # only the reference arm B touched it
    assert h._divergence(set(), set()) == {}    # nothing touched -> empty map


def test_file_category_lookup_table():
    cases = {
        "test_parser.py": "tests", "src/foo.test.js": "tests",
        "README.md": "docs", "docs/guide.md": "docs",
        "package.json": "manifest", "pyproject.toml": "manifest", "go.mod": "manifest",
        "poetry.lock": "lockfile", "deps.lock": "lockfile",
        ".github/workflows/ci.yml": "ci", "Dockerfile": "ci", "deploy.yaml": "ci",
        "src/parser.py": "source", "main.go": "source",
    }
    for path, cat in cases.items():
        assert h._file_category(path) == cat, path


def test_change_shape_lookup_table():
    assert h._change_shape("A", 40, 0) == "new file"
    assert h._change_shape("D", 0, 12) == "deleted"
    assert h._change_shape("R", 1, 1) == "renamed"
    assert h._change_shape("M", 5, 0) == "pure additions"
    assert h._change_shape("M", 0, 5) == "pure deletions"
    assert h._change_shape("M", 30, 20) == "50-line rewrite"   # >= _REWRITE_LINES
    assert h._change_shape("M", 2, 1) == "targeted edit"


def test_beat_caption_is_factual_with_category_shape_churn_and_arm():
    cap = h._beat_caption("test_parser.py", "A", 40, 0, "only my-skill")
    for needle in ("tests", "new file", "+40", "only my-skill"):
        assert needle in cap, needle


def _card(tests, add, dele, *, tests_file=False):
    fm = {"test_x.py": {}} if tests_file else {"x.py": {}}
    return {"tests": tests, "lint": None, "build": None, "add": add, "del": dele,
            "turns": 5, "cost": 0.04, "files": len(fm), "fmeta": fm}


def test_thesis_template_selection_on_score_and_size_deltas():
    # A passes tests where B fails -> green-vs-red, and 'test-backed' since A touched a test
    t = h._thesis_text(_card(True, 7, 2, tests_file=True), _card(False, 1, 1),
                       "skill", "ctrl")
    assert "skill" in t and "green" in t and "test-backed" in t
    # both pass, A meaningfully larger -> larger change
    assert "larger" in h._thesis_text(_card(True, 30, 0), _card(True, 2, 0),
                                      "skill", "ctrl")
    # both pass, within the size margin -> no measurable difference
    assert h._thesis_text(_card(True, 4, 0), _card(True, 3, 0), "skill", "ctrl") \
        == "no measurable difference on this task"
    # only one arm has a run / neither has a run
    assert "only the" in h._thesis_text(_card(True, 1, 0), None, "skill", "ctrl")
    assert h._thesis_text(None, None, "s", "c") == "no valid runs to compare on this task"


def test_diffofdiffs_counts_agree_a_only_b_only():
    # both arms delete '-old'; A adds '+new', B adds '+other' -> agree on the delete,
    # diverge on the add (a replace tagged A-only by the heuristic).
    ra = _rr(Arm.SKILL_ON, True)
    rb = _rr(Arm.SKILL_OFF, None)
    ra.diff = "diff --git a/x.py b/x.py\n@@ -1,1 +1,1 @@\n-old\n+new\n"
    rb.diff = "diff --git a/x.py b/x.py\n@@ -1,1 +1,1 @@\n-old\n+other\n"
    dod = h._diffofdiffs(ra, rb, ["x.py"])["x.py"]
    assert dod["agree"] == 1 and dod["a_only"] == 1 and dod["b_only"] == 0
    assert h._diffofdiffs(None, None, ["x.py"])["x.py"] == \
        {"agree": 0, "a_only": 0, "b_only": 0}


def _divmap_diffs():
    """ON touches util.py (both) + test_util.py (skill-only); OFF touches util.py
    (both, differently) + README.md (ctrl-only) -> one chip of each divergence class."""
    diff_on = ("diff --git a/util.py b/util.py\n--- a/util.py\n+++ b/util.py\n"
               "@@ -1,1 +1,1 @@\n-old\n+new\n"
               "diff --git a/test_util.py b/test_util.py\nnew file mode 100644\n"
               "--- /dev/null\n+++ b/test_util.py\n@@ -0,0 +1,1 @@\n+def test_x(): pass\n")
    diff_off = ("diff --git a/util.py b/util.py\n--- a/util.py\n+++ b/util.py\n"
                "@@ -1,1 +1,1 @@\n-old\n+other\n"
                "diff --git a/README.md b/README.md\nnew file mode 100644\n"
                "--- /dev/null\n+++ b/README.md\n@@ -0,0 +1,1 @@\n+docs\n")
    res = _two_task_results()
    for r in res:
        r.diff = diff_on if r.arm is Arm.SKILL_ON else diff_off
    return res


def test_contrast_band_renders_scorecards_divmap_and_resolvable_chip_targets():
    cfg = _cfg()
    man = h.experiment_manifest(cfg, timestamp=1.0)
    doc = h.build_html_report(_divmap_diffs(), h.Preflight(), cfg, man)
    assert doc.count('class="contrast"') == 2           # one band per task
    assert doc.count('class="scorecard"') == 4          # 2 arms x 2 tasks
    for cls in ('class="chip both"', 'class="chip skill-only"',
                'class="chip ctrl-only"'):
        assert cls in doc, cls
    # a diff-of-diffs summary appears for the shared file
    assert 'class="dod"' in doc
    # every chip data-target addresses a real rendered file node
    ids = set(re.findall(r'id="(f-[^"]+)"', doc))
    targets = re.findall(r'data-target="(f-[^"]+)"', doc)
    assert targets and all(t in ids for t in targets)
    # skill-only chip targets the skill_on pane; ctrl-only targets the skill_off pane
    assert any("skill-on" in t for t in targets)
    assert any("skill-off" in t for t in targets)


def test_contrast_band_escapes_hostile_filename_and_never_uses_data_tip():
    # LOAD-BEARING (plan 024 §1.4): a hostile path rides the chip via title= (plain-text
    # native tooltip), NEVER data-tip -- the tooltip wiring assigns data-tip through
    # innerHTML, so a filename there would re-parse as live markup. The live tag must
    # not survive; only its escaped form, and the band must carry no data-tip at all.
    cfg = _cfg()
    man = h.experiment_manifest(cfg, timestamp=1.0)
    evil = '"><script>.py'                              # no spaces -> survives the git regex
    diff = (f"diff --git a/{evil} b/{evil}\n--- a/{evil}\n+++ b/{evil}\n"
            f"@@ -1,1 +1,1 @@\n-a\n+b\n")
    doc = h.build_html_report(_results_with_diff(diff), h.Preflight(), cfg, man)
    assert 'class="contrast"' in doc and 'class="chip' in doc
    assert '"><script>' not in doc                      # attribute/tag breakout blocked
    assert "&lt;script&gt;" in doc                       # escaped form present
    band = doc[doc.index('class="contrast"'):doc.index('class="cmp-bar"')]
    assert "title=" in band and "data-tip=" not in band


def test_contrast_band_is_arm_symmetric_for_three_arms():
    cfg = _cfg(skill_name="skill-a", skill_b_src=Path("/s/b"), skill_b_name="skill-b")
    man = h.experiment_manifest(cfg, timestamp=1.0, offline=True)
    res = _three_arm_results()
    for r in res:                                       # every arm touches the same file
        r.diff = "diff --git a/x.py b/x.py\n@@ -1,1 +1,1 @@\n-a\n+b\n"
    doc = h.build_html_report(res, h.Preflight(), cfg, man)
    assert 'class="contrast"' in doc and 'class="thesis"' in doc
    assert doc.count('class="scorecard"') == 6          # 3 arms x 2 tasks
    for av in ("skill_on", "skill_b", "skill_off"):
        assert f'<div class="scorecard" data-arm="{av}">' in doc


def test_diffviewer_wires_divmap_chip_clicks_escape_safe():
    cfg = _cfg()
    man = h.experiment_manifest(cfg, timestamp=1.0)
    doc = h.build_html_report(_two_task_results(), h.Preflight(), cfg, man)
    script = re.search(r"<script>(.+?)</script>", doc, re.S).group(1)
    dv = script[script.index("function DiffViewer("):]
    assert ".divmap .chip" in dv
    assert 'getAttribute("data-target")' in dv
    assert re.search(r"innerHTML\s*=", dv) is None      # never innerHTML of diff text


# --------------------------------------------------------------------------
# Cost / power (plan 006)
# --------------------------------------------------------------------------

def test_estimate_cost_math():
    cfg = _cfg(k=5)
    tasks = [h.Task(id="a", prompt="x"), h.Task(id="b", prompt="y")]
    e = h.estimate_cost(cfg, tasks, per_run_usd=0.31, per_run_seconds=40.0)
    assert e["n_runs"] == 20 and e["n_judge_calls"] == 0
    assert round(e["projected_usd"], 2) == 6.20 and e["projected_wall_seconds"] is not None
    cfg2 = _cfg(k=5, judge_enabled=True, judge_max_pairs_per_task=3)
    e2 = h.estimate_cost(cfg2, tasks, per_run_usd=0.10)
    assert e2["n_judge_calls"] == 2 * 3 * 2 and round(e2["projected_usd"], 2) == round(0.10 * 32, 2)


def test_should_stop_ceiling():
    assert h._should_stop(10.0, 5.0) is True
    assert h._should_stop(4.0, 5.0) is False
    assert h._should_stop(99.0, None) is False


def test_mde_monotone():
    cfg = _cfg(k=6)
    small = h.minimum_detectable_effect(cfg, 3, 0.5, 0.02, sims=60)
    big = h.minimum_detectable_effect(cfg, 3, 0.5, 0.40, sims=60)
    assert small is not None and (big is None or big >= small)
    assert small in [i / 100 for i in range(1, 51)]   # returns a grid delta


def test_mde_bisection_matches_linear_scan():
    # Bisection is only valid on a monotone power curve. On a guaranteed-monotone
    # curve it must return EXACTLY what a brute linear scan returns -- the real
    # finite-sim curve is only monotone in expectation (see test_mde_monotone).
    grid = [i / 100 for i in range(1, 21)]            # 0.01 .. 0.20

    def brute(power_at, power):
        return next((grid[i] for i in range(len(grid)) if power_at(i) >= power), None)

    def step_at(t):                                   # avoid E731 (assigned lambda)
        def f(i):
            return 1.0 if i >= t else 0.0
        return f

    for thr in range(len(grid) + 1):                  # thr == len(grid) => None
        step = step_at(thr)
        assert h._smallest_delta_reaching_power(grid, step, 0.8) == brute(step, 0.8)

    def ramp(i):
        return i / (len(grid) - 1)                    # 0.0 .. 1.0, monotone
    assert h._smallest_delta_reaching_power(grid, ramp, 0.8) == brute(ramp, 0.8)


def test_mde_returns_none_when_undetectable():
    grid = [i / 100 for i in range(1, 21)]
    # Real path: tiny grid, huge noise -> no delta reaches 80% power.
    assert h.minimum_detectable_effect(_cfg(k=6), 2, 0.5, 5.0, deltas=grid,
                                       sims=40) is None
    # Search path: a flat power curve below target -> None.
    assert h._smallest_delta_reaching_power(grid, lambda i: 0.5, 0.8) is None


def test_noise_floor_false_positive_rate_is_calibrated():
    # SPIKE (plan 019) OFF-vs-OFF premise: two controls drawn from the SAME center
    # produce a CALIBRATED spurious-"significant" rate (~alpha), so a rate far above
    # the floor in a real run would be a genuine signal, not noise.
    rng = random.Random(7)
    cfg = _cfg(k=6, bootstrap_alpha=0.05)
    tasks = ["t0", "t1", "t2"]
    sd, center, trials, excl = 0.2, 0.5, 300, 0

    def draw():
        return {t: [center + rng.gauss(0, sd) for _ in range(cfg.k)] for t in tasks}

    for _ in range(trials):
        lo, hi, clustered = h.cluster_bootstrap_ci(
            draw(), draw(), tasks, 400, cfg.bootstrap_alpha, rng)
        assert clustered  # >=2 tasks -> conservative clustered path, not flat
        if not (lo <= 0 <= hi):
            excl += 1
    rate = excl / trials
    assert rate < 0.15, f"false-positive rate {rate} too high for equal-mean controls"


def test_two_controls_collapse_under_current_arm_labels():
    # SPIKE GUARD (plan 019): today there is exactly ONE control identity, and every
    # control labels as "control". A noise-floor mode therefore CANNOT reuse the
    # existing control arm for both sides -- it needs a distinct second control
    # identity + distinct labels (control_a/control_b). This pins that.
    cfg = _cfg()
    assert h.arm_label(cfg, Arm.SKILL_OFF) == "control"
    assert h.arm_skill(cfg, Arm.SKILL_OFF) == (None, None)
    assert h.pair_key(cfg, Arm.SKILL_OFF, Arm.SKILL_OFF) == "control_vs_control"
    raised = False
    try:
        _cfg(skill_b_src=Path("/skills/x"))  # name omitted -> half-set, rejected
    except ValueError:
        raised = True
    assert raised, "skill_b without a name should be rejected (cannot be a 2nd control)"


# --------------------------------------------------------------------------
# CI gate (plan: ci)
# --------------------------------------------------------------------------

def test_ci_exit_code_policies():
    cfg = _cfg()
    man = h.experiment_manifest(cfg, timestamp=1.0)
    win = h.summary_dict(_two_task_results(1.0, 0.0), cfg, man)   # clear improvement
    flat = h.summary_dict(_two_task_results(1.0, 1.0), cfg, man)  # no difference
    assert h.ci_exit_code(win, cfg, "no-regression")[0] == 0
    assert h.ci_exit_code(win, cfg, "require-improvement")[0] == 0
    # flat: passes no-regression but fails require-improvement
    assert h.ci_exit_code(flat, cfg, "no-regression")[0] == 0
    assert h.ci_exit_code(flat, cfg, "require-improvement")[0] == 1


# --------------------------------------------------------------------------
# Demo offline (plan 005) + packaging-independence
# --------------------------------------------------------------------------

def test_demo_results_earns_significant_offline():
    res = h._demo_results()
    assert len(res) == 24 and all(r.agent_ok for r in res)
    cfg = h.ExperimentConfig(repo_path=Path("."), base_ref="HEAD",
                             skill_src=Path("demo_skill"), skill_name="write-tests-first",
                             results_dir=Path(tempfile.mkdtemp()), k=6, bootstrap_iters=500,
                             permutation_iters=500)
    man = h.experiment_manifest(cfg, timestamp=0.0, offline=True)
    assert man["claude_cli_version"] is None and man["base_ref_sha"] is None  # tool-free
    s = h.summary_dict(res, cfg, man)
    v = h.primary_verdict(s, cfg)
    assert v["label"] == "significant" and v["direction"] == 1   # favours the skill arm
    # summary q-values are populated for secondary metrics (BH applied)
    assert s["itt"]["lint_pass"]["q_value"] is not None


def test_demo_command_writes_offline():
    out = Path(tempfile.mkdtemp())
    h.cmd_demo(out)
    doc = (out / "report.html").read_text()
    assert doc.startswith("<!doctype html") and "</html>" in doc and "<table" in doc
    assert (out / "summary.json").exists()
    assert not (out / "badge.svg").exists()      # the badge artifact is retired


def test_demo_live_flag_removed():
    # --live was a no-op that always errored; it must no longer be a known arg.
    try:
        h.main(["demo", "--live", "-o", tempfile.mkdtemp()])
        assert False, "expected SystemExit for unknown --live arg"
    except SystemExit as e:
        assert e.code == 2  # argparse "unrecognized arguments" exit code


# --------------------------------------------------------------------------
# Resume robustness (fixes from verification pass)
# --------------------------------------------------------------------------

def test_load_results_tolerates_corrupt_trailing_line():
    p = Path(tempfile.mkdtemp()) / "results.jsonl"
    good = h.RunResult(task_id="t", arm=Arm.SKILL_ON, run_index=0, worktree=Path("/wt"),
                       skill_activated=True, activation_reason="", agent_ok=True,
                       completed=True, scores={"tests_pass": 1.0})
    p.write_text(json.dumps(good.to_dict()) + "\n" + '{"task_id": "t", "ar')  # partial
    loaded = h.load_results(p)
    assert len(loaded) == 1 and loaded[0].task_id == "t"   # partial line skipped, no crash


def test_should_stop_pretrips_over_budget():
    # The resume seed must pre-trip the ceiling when prior spend already exceeds it.
    assert h._should_stop(12.0, 10.0) is True and h._should_stop(8.0, 10.0) is False


def test_budget_guard_bounds_spend():
    # The fix: the budget gate lives INSIDE the semaphore, so once the ceiling
    # trips, queued runs skip instead of all spending. Stub execute_run (mirroring
    # the real gate placement) + the git/claude-touching helpers so this is pure.
    import asyncio
    cfg = _cfg(k=10, max_concurrency=2, cost_ceiling_usd=0.25,
               results_dir=Path(tempfile.mkdtemp()))
    tasks = [h.Task(id="a", prompt="x")]
    orig = (h.run_preflight, h.execute_run, h.experiment_manifest)
    h.run_preflight = lambda c, t, s: h.Preflight()
    h.experiment_manifest = lambda c, seed=0, timestamp=None, offline=False, tasks=None: {
        "skill_md_sha256": None, "harness_version": "t", "model": c.model,
        "base_ref_sha": None, "k": c.k, "seed": seed, "timestamp": timestamp}
    spent_runs = {"n": 0}

    async def fake_execute(task, arm, idx, cfg, scorers, pf, sem, budget=None):
        async with sem:                                  # gate inside the slot
            if budget is not None and budget["stop"]:
                return h._failed_result(task, arm, idx, cfg, "ceiling", h._SKIPPED_ERR)
            spent_runs["n"] += 1
            await asyncio.sleep(0)
            return h.RunResult(task_id=task.id, arm=arm, run_index=idx,
                               worktree=Path("/wt"), skill_activated=True,
                               activation_reason="", agent_ok=True, completed=True,
                               cost_usd=0.05, scores={"tests_pass": 1.0})
    h.execute_run = fake_execute
    try:
        results, _pf = asyncio.run(h.run_experiment(cfg, tasks))
    finally:
        h.run_preflight, h.execute_run, h.experiment_manifest = orig
    # 20 cells total; ceiling 0.25 at $0.05/run -> ~5 spent, overshoot <= concurrency.
    assert spent_runs["n"] < 20, spent_runs["n"]
    assert sum((r.cost_usd or 0) for r in results) <= 0.25 + 0.05 * cfg.max_concurrency


def test_execute_run_emits_run_skipped_when_budget_tripped():
    # A pre-tripped budget must make execute_run emit a run_skipped event (so the live
    # grid marks the cell instead of leaving it stuck on "pending") AND return a
    # _SKIPPED_ERR result. The gate returns BEFORE any worktree/git work, so the real
    # execute_run is driven here with no git/claude needed.
    import asyncio
    cfg = _cfg()
    task = h.Task(id="a", prompt="x")
    events: list[dict] = []

    async def drive():
        sem = asyncio.Semaphore(1)
        return await h.execute_run(task, Arm.SKILL_ON, 0, cfg, [], h.Preflight(),
                                   sem, budget={"spent": 9.9, "stop": True},
                                   on_event=events.append)

    res = asyncio.run(drive())
    assert res.error == h._SKIPPED_ERR
    skipped = [e for e in events if e.get("type") == "run_skipped"]
    assert len(skipped) == 1 and skipped[0]["label"] == "a-skill_on-0"
    assert skipped[0]["reason"]                       # carries a human reason for the UI
    assert not any(e.get("type") == "run_start" for e in events)   # a skip never starts


def test_head_to_head_requires_both_skill_b_fields():
    try:
        _cfg(skill_b_src=Path("/s/b"))      # name missing -> degenerate; must raise
        assert False, "should have raised"
    except ValueError:
        pass
    # both set is fine
    assert h._is_head_to_head(_cfg(skill_b_src=Path("/s/b"), skill_b_name="b"))


def test_demo_pp_excludes_non_activated_on_run():
    on = [r for r in h._demo_results() if r.arm is Arm.SKILL_ON]
    assert all(r.arm_skill_name == "write-tests-first" for r in on)
    # exactly one demo ON run did not activate -> per-protocol must drop it
    assert sum(1 for r in on if r.pp_valid) == len(on) - 1


def test_aggregate_judge_double_failure_not_consistent():
    comps = [_cmp("t", 0, "a_first", None), _cmp("t", 0, "b_first", None)]
    agg = h.aggregate_judge(comps)["my-skill_vs_control"]
    assert agg["failed"] == 2 and agg["total_pairs"] == 0 and agg["consistent"] == 0


def test_judge_winrate_ci_brackets_point():
    # 3 my-skill wins + 1 control win across 2 tasks -> win_rate_a = 0.75; the
    # cluster-bootstrap CI must bracket the point estimate and stay in [0, 1].
    comps = [_cmp("t1", 0, "a_first", "my-skill"), _cmp("t1", 1, "a_first", "my-skill"),
             _cmp("t2", 0, "a_first", "my-skill"), _cmp("t2", 1, "a_first", "control")]
    agg = h.aggregate_judge(comps, ci_iters=2000, alpha=0.05,
                            rng=random.Random(0))["my-skill_vs_control"]
    assert agg["win_rate_a"] == 0.75              # point estimate unchanged
    ci = agg["win_rate_a_ci"]
    assert isinstance(ci, tuple) and len(ci) == 2
    lo, hi = ci
    assert 0.0 <= lo <= agg["win_rate_a"] <= hi <= 1.0


def test_judge_winrate_ci_none_when_all_ties():
    # decisive == 0 (every vote a tie) -> win_rate_a is nan and the CI is None,
    # with no ZeroDivisionError (degenerate case; ties into plan 007).
    comps = [_cmp("t", 0, "a_first", "tie"), _cmp("t", 0, "b_first", "tie")]
    agg = h.aggregate_judge(comps, ci_iters=500, alpha=0.05,
                            rng=random.Random(0))["my-skill_vs_control"]
    assert agg["win_rate_a"] != agg["win_rate_a"]     # nan
    assert agg["win_rate_a_ci"] is None


def test_aggregate_judge_default_has_no_ci():
    # Backward compat: callers that don't ask for a CI get win_rate_a_ci == None.
    comps = [_cmp("t", 0, "a_first", "my-skill"), _cmp("t", 0, "b_first", "my-skill")]
    assert h.aggregate_judge(comps)["my-skill_vs_control"]["win_rate_a_ci"] is None


def test_judge_report_shows_ci():
    # A clean sweep (win_rate 1.0 over two tasks) -> CI [1.000, 1.000] in the text.
    cfg = _cfg(bootstrap_iters=200)
    comps = [_cmp("a", 0, "a_first", "my-skill"), _cmp("a", 0, "b_first", "my-skill"),
             _cmp("b", 0, "a_first", "my-skill"), _cmp("b", 0, "b_first", "my-skill")]
    rep = h.build_judge_report(comps, cfg)
    assert "win rate: 1.000 [1.000, 1.000]" in rep


def test_redact_names_scrubs_skill_names_from_diffs():
    out = h._redact_names("calls Resolve-Parallel and control here",
                          {"resolve-parallel", "control"})
    assert "resolve-parallel" not in out.lower() and "the-skill" in out


# --------------------------------------------------------------------------
# `skill-test` quick CLI (resolution / target / inject mode)
# --------------------------------------------------------------------------

def test_resolve_skill_case_insensitive_and_not_found():
    d = Path(tempfile.mkdtemp()) / ".claude" / "skills" / "my-skill"
    d.mkdir(parents=True)
    (d / "Skill.md").write_text("# guide")          # mixed-case filename
    md = h.resolve_skill("my-skill", project_dir=d.parent.parent.parent)
    assert md.name == "Skill.md"
    try:
        h.resolve_skill("definitely-not-a-real-skill-xyz")
        assert False, "should raise"
    except SystemExit:
        pass


def test_list_available_skills_enumerates_and_dedups():
    proj = Path(tempfile.mkdtemp())
    home = Path(tempfile.mkdtemp())

    def mk(root, name):
        d = root / ".claude" / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("# x")

    mk(proj, "alpha")                          # project only
    mk(home, "beta")                           # global only
    mk(proj, "shared")
    mk(home, "shared")                         # in both -> project wins (precedence)
    out = h.list_available_skills(project_dir=proj, home=home)
    by = {s["name"]: s for s in out}
    assert by["alpha"]["source"] == "project"
    assert by["beta"]["source"] == "global"
    assert by["shared"]["source"] == "project"   # resolve precedence: project first
    assert [s["name"] for s in out] == sorted(s["name"] for s in out)  # name-sorted


def test_prepare_skill_guidance_strips_frontmatter_and_dynamic_blocks():
    p = Path(tempfile.mkdtemp()) / "SKILL.md"
    p.write_text("---\nname: x\ndescription: y\n---\n# Body\nrun !`git diff HEAD` now\n")
    g = h.prepare_skill_guidance(p)
    assert "name: x" not in g and "git diff" not in g and "# Body" in g


def test_pr_url_and_command_detection():
    m = h._PR_RE.search("https://github.com/cloverleaf-coach/cloverleaf-client/pull/7044")
    assert m.group(1) == "cloverleaf-coach/cloverleaf-client" and m.group(2) == "7044"
    npm = Path(tempfile.mkdtemp())
    (npm / "package.json").write_text("{}")
    assert h._detect_commands(npm) == ("npm ci", "npm test --silent")
    go = Path(tempfile.mkdtemp())
    (go / "go.mod").write_text("module x")
    assert h._detect_commands(go) == ("go mod download", "go test ./...")


def test_inject_leak_detects_skill_tool_use():
    leak = [{"type": "assistant", "message": {"content":
            [{"type": "tool_use", "name": "Skill", "input": {}}]}}]
    assert h._inject_leak(leak) == "skill-leak"
    assert h._inject_leak([{"type": "assistant", "message": {"content": []}}]) is None


def test_build_quick_wires_inject_and_arms():
    import types
    orig = (h.resolve_skill, h.resolve_target)
    h.resolve_skill = lambda name, project_dir=None: Path(f"/skills/{name}/SKILL.md")
    h.resolve_target = lambda target, prompt=None: (Path("/repo"), "feature-branch",
                                                    "resolve comments", "npm ci", "npm test")
    try:
        a = types.SimpleNamespace(skill_a="resolve-parallel", skill_b="resolve-comments",
                                  target="https://github.com/x/y/pull/1", prompt=None,
                                  test_cmd=None, setup_cmd=None, k=3, no_judge=False,
                                  html=Path("r.html"), worktree_isolation=False,
                                  trust_remote=True)
        cfg, tasks = h._build_quick(a)
        assert cfg.isolation == "inject" and cfg.skill_name == "resolve-parallel"
        assert cfg.skill_b_name == "resolve-comments" and h._is_head_to_head(cfg)
        assert cfg.skill_src == Path("/skills/resolve-parallel")
        assert cfg.base_ref == "feature-branch"
        assert tasks[0].test_cmd == "npm test" and tasks[0].prompt == "resolve comments"
        # "none" -> 2-arm (skill vs control)
        a.skill_b = "none"
        cfg2, _ = h._build_quick(a)
        assert cfg2.skill_b_name is None and not h._is_head_to_head(cfg2)
    finally:
        h.resolve_skill, h.resolve_target = orig


def test_prepare_guidance_keeps_normal_inline_code():
    # the dynamic-block strip must NOT swallow ordinary backticked code / exclamations
    p = Path(tempfile.mkdtemp()) / "SKILL.md"
    p.write_text("Use `array.map()` and be careful! Then `final` step. run !`pwd` now\n")
    g = h.prepare_skill_guidance(p)
    assert "`array.map()`" in g and "`final`" in g and "be careful!" in g
    assert "pwd" not in g                            # the !`pwd` block IS stripped


def test_pr_target_requires_trust_remote():
    import types
    a = types.SimpleNamespace(skill_a="sa", skill_b="none",
                              target="https://github.com/x/y/pull/1", prompt=None,
                              test_cmd=None, setup_cmd=None, k=3, no_judge=False,
                              html=Path("r.html"), worktree_isolation=False, trust_remote=False)
    try:
        h._build_quick(a)
        assert False, "PR target without --trust-remote must be refused"
    except SystemExit:
        pass


def test_unsafe_owner_repo_rejected():
    try:
        h.resolve_target("https://github.com/-evil/repo/pull/1", prompt="x")
        assert False, "should reject dash-leading owner"
    except SystemExit:
        pass


def test_directory_target_requires_a_prompt():
    # A path target has no auto-prompt (only a PR does), so it must be given one. This is
    # the contract the web form now enforces with its required Task-prompt field; the
    # engine is the backstop. A prompt threads straight through to the task.
    try:
        h.resolve_target(".", prompt=None)
        assert False, "a directory target with no prompt must be refused"
    except SystemExit:
        pass
    repo, ref, prompt, _setup, _test = h.resolve_target(".", prompt="Add input validation")
    assert prompt == "Add input validation"        # same prompt will run on every arm


def test_skill_a_equals_skill_b_rejected():
    try:
        _cfg(skill_b_src=Path("/s/b"), skill_b_name="my-skill")   # == skill_name, same model
        assert False, "should reject skill_b == skill_a"
    except ValueError:
        pass


def test_same_skill_different_model_allowed_for_model_comparison():
    # Same skill on both arms is fine when the MODELS differ (a model comparison).
    cfg = _cfg(skill_b_src=Path("/s/b"), skill_b_name="my-skill",
               model_a="claude-sonnet-4-6", model_b="claude-opus-4-8")
    assert h.arm_model(cfg, Arm.SKILL_ON) == "claude-sonnet-4-6"
    assert h.arm_model(cfg, Arm.SKILL_B) == "claude-opus-4-8"
    # labels disambiguate by model so they don't collide
    assert h.arm_label(cfg, Arm.SKILL_ON) == "my-skill @ sonnet"
    assert h.arm_label(cfg, Arm.SKILL_B) == "my-skill @ opus"


def test_per_arm_model_comparison_shape_and_backcompat():
    # include_control=False -> pure A vs B (two arms, one pair, no control).
    cfg = _cfg(skill_b_src=Path("/s/b"), skill_b_name="my-skill",
               model_a="claude-sonnet-4-6", model_b="claude-opus-4-8",
               include_control=False)
    arms = [h.arm_label(cfg, a) for a in h.experiment_arms(cfg)]
    assert arms == ["my-skill @ sonnet", "my-skill @ opus"]      # no control arm
    pairs = h.experiment_pairs(cfg)
    assert len(pairs) == 1 and pairs[0] == h.primary_pair(cfg)
    # Backward compat: NO overrides -> labels are the plain skill name / control.
    base = _cfg()
    assert h.arm_label(base, Arm.SKILL_ON) == "my-skill"
    assert h.arm_label(base, Arm.SKILL_OFF) == "control"
    assert h.arm_model(base, Arm.SKILL_ON) == base.model


# --------------------------------------------------------------------------
# Pluggable CLI runner (plan 022)
# --------------------------------------------------------------------------

def test_runner_b_triggers_head_to_head_and_labels_by_cli():
    # A second runner (no skill_b) is still a head-to-head: skill A on claude vs codex.
    cfg = _cfg(runner_b="codex")
    assert h._is_head_to_head(cfg)
    arms = [h.arm_label(cfg, a) for a in h.experiment_arms(cfg)]
    assert arms == ["my-skill", "codex", "control"]
    # command arms have NO claude model (model-axis isolation)
    assert h.arm_runner(cfg, Arm.SKILL_B) == "codex"
    assert h.arm_model(cfg, Arm.SKILL_B) is None
    assert h.arm_runner(cfg, Arm.SKILL_ON) is None
    assert h._runners_vary(cfg)


def test_runner_label_resolves_registry_command_and_default():
    assert h._runner_label(None) == "claude"          # built-in
    assert h._runner_label("codex") == "codex"        # registry
    assert h._runner_label("my-cli --flag run") == "my-cli"   # raw command -> 1st token


def test_pair_is_cross_runner_only_when_runners_differ():
    cfg = _cfg(runner_b="codex")
    assert h._pair_is_cross_runner(cfg, Arm.SKILL_ON, Arm.SKILL_B)     # claude vs codex
    assert not h._pair_is_cross_runner(cfg, Arm.SKILL_ON, Arm.SKILL_OFF)  # both claude
    base = _cfg()
    assert not h._pair_is_cross_runner(base, Arm.SKILL_ON, Arm.SKILL_OFF)


def test_cross_runner_label_collision_is_rejected():
    # Two command arms whose runner labels collide must be refused (ambiguous report).
    try:
        _cfg(runner_a="codex", skill_b_src=Path("/s/b"), skill_b_name="b",
             runner_b="codex exec")
        assert False, "colliding 'codex' runner labels should raise"
    except ValueError:
        pass


def test_command_arm_skips_model_in_models_vary():
    # codex arm has no claude model -> it must not be counted as a model difference,
    # so a single-claude-model run with a codex arm is NOT a model comparison.
    cfg = _cfg(runner_b="codex")
    assert not h._models_vary(cfg)
    assert h.arm_label(cfg, Arm.SKILL_ON) == "my-skill"   # no "@ model" suffix


def test_run_command_agent_preset_feeds_prompt_via_stdin():
    # Free: register a temp preset whose argv is `cat` (echoes stdin). If the prompt
    # reaches the transcript, it was delivered on STDIN -- the injection-safe path the
    # codex preset uses (prompt text NEVER interpolated into a shell command).
    import asyncio
    import tempfile

    injection = "hello; rm -rf / # $(touch pwned)"
    h._RUNNERS["_test_echo"] = {"label": "_test_echo", "argv": ["cat"]}
    try:
        with tempfile.TemporaryDirectory() as d:
            wt = Path(d)
            task = h.Task(id="t", prompt=injection)
            cfg = _cfg(runner_a="_test_echo")
            res = asyncio.run(h.run_command_agent(wt, task, cfg, "_test_echo", 30))
            # the dangerous string round-trips verbatim through stdin -> no shell parsed it
            assert res["transcript"] == injection
            assert not (wt / "pwned").exists()       # the $(...) never executed
    finally:
        del h._RUNNERS["_test_echo"]
    # exposure-only shape: ran to completion, claude-only fields cleared
    assert res["ok"] is True
    assert res["completed"] is True and res["timed_out"] is False
    assert res["cost_usd"] is None and res["num_turns"] is None
    assert res["events"] == []
    assert res["returncode"] == 0


def test_run_command_agent_template_quotes_prompt_file_path():
    # The raw-template path substitutes a SHELL-QUOTED temp-file path for {prompt_file},
    # never the prompt text. A trivial template that cats the file proves the prompt is
    # delivered out-of-band (file), so a malicious prompt can't break out of the shell.
    import asyncio
    import tempfile

    injection = "$(touch pwned); echo hi"
    with tempfile.TemporaryDirectory() as d:
        wt = Path(d)
        task = h.Task(id="t", prompt=injection)
        runner = "cat {prompt_file}"
        cfg = _cfg(runner_a=runner)
        res = asyncio.run(h.run_command_agent(wt, task, cfg, runner, 30))
        assert res["transcript"] == injection        # file content, unexecuted
        assert not (wt / "pwned").exists()
    assert res["ok"] is True and res["returncode"] == 0


def test_run_command_agent_exposure_only_ok_on_nonzero_exit():
    # ok is EXPOSURE-only: a nonzero exit is an OUTCOME (captured by tests_pass), not a
    # reason to drop the run. ok stays True as long as the process actually ran.
    import asyncio
    import tempfile

    h._RUNNERS["_test_fail"] = {"label": "_test_fail", "argv": ["sh", "-c", "exit 3"]}
    try:
        with tempfile.TemporaryDirectory() as d:
            res = asyncio.run(
                h.run_command_agent(Path(d), h.Task(id="t", prompt="x"), _cfg(),
                                    "_test_fail", 30))
    finally:
        del h._RUNNERS["_test_fail"]
    assert res["returncode"] == 3
    assert res["ok"] is True            # nonzero exit is NOT censored
    assert res["timed_out"] is False


def test_task_id_path_traversal_rejected():
    try:
        h.Task(id="../evil", prompt="x")
        assert False, "should reject task id with a path separator"
    except ValueError:
        pass


def test_task_id_normal_accepted():
    # The exact ids the harness itself uses must still pass.
    for good in ("task", "add-email-validation", "fix-null-deref", "v1.2_run-3"):
        h.Task(id=good, prompt="x")


def test_pr_target_denies_github_mutation():
    import types
    orig = (h.resolve_skill, h.resolve_target)
    h.resolve_skill = lambda name, project_dir=None: Path(f"/skills/{name}/SKILL.md")
    h.resolve_target = lambda target, prompt=None: (Path("/r"), "ref", "do", None, "pytest")
    try:
        a = types.SimpleNamespace(
            skill_a="a", skill_b="none", target="https://github.com/x/y/pull/7044",
            prompt=None, test_cmd=None, setup_cmd=None, k=3, no_judge=False,
            html=Path("r.html"), worktree_isolation=False, trust_remote=True)
        cfg, _ = h._build_quick(a)
        assert "Bash(gh:*)" in cfg.disallowed_tools         # no replying / resolving threads
        assert "Bash(git push:*)" in cfg.disallowed_tools   # no pushing
        a.target = "."                                      # non-PR -> no deny rules
        cfg2, _ = h._build_quick(a)
        assert cfg2.disallowed_tools == ()
    finally:
        h.resolve_skill, h.resolve_target = orig


def test_single_task_inconclusive_warning():
    cfg = _cfg()
    res = []
    for arm, v in ((Arm.SKILL_ON, 1.0), (Arm.SKILL_OFF, 0.0)):
        r = _rr(arm, arm is Arm.SKILL_ON, scores={"tests_pass": v})
        r.task_id = "only"
        res.append(r)
    man = h.experiment_manifest(cfg, timestamp=1.0, offline=True)
    rep = h.build_report(res, h.Preflight(), cfg, manifest=man)
    assert "Single task" in rep and "inconclusive" in rep


import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402


def _noisy_three_arm_results():
    """3-arm with WITHIN-arm variance so the bootstrap CI depends on the RNG
    order -- the only way to expose the cross-renderer divergence plan 010 fixes."""
    jit = random.Random(123)
    res = []
    base = {Arm.SKILL_ON: 0.8, Arm.SKILL_B: 0.55, Arm.SKILL_OFF: 0.2}
    names = {Arm.SKILL_ON: "skill-a", Arm.SKILL_B: "skill-b", Arm.SKILL_OFF: None}
    for t in ("A", "B"):
        for arm, b in base.items():
            for i in range(4):
                v = b + jit.uniform(-0.15, 0.15)
                r = RunResult(task_id=t, arm=arm, run_index=i, worktree=Path("/wt"),
                              skill_activated=(True if names[arm] else None),
                              activation_reason="", agent_ok=True, completed=True,
                              cost_usd=round(0.05 + 0.01 * i, 3), arm_skill_name=names[arm],
                              scores={"tests_pass": v, "lint_pass": 0.5 + 0.5 * (i % 2),
                                      "build_pass": 1.0, "diff_lines": 30.0 + 10 * v,
                                      "cost_usd": round(0.05 + 0.01 * i, 3)})
                res.append(r)
    return res


def test_estimates_identical_across_summary_and_html():
    cfg = _cfg(skill_name="skill-a", skill_b_src=Path("/s/b"), skill_b_name="skill-b")
    res = _noisy_three_arm_results()
    man = h.experiment_manifest(cfg, timestamp=1.0, offline=True)
    seed = 7
    s = h.summary_dict(res, cfg, man, seed=seed)
    doc = h.build_html_report(res, h.Preflight(), cfg, man, seed=seed)
    blob = json.loads(doc.split("window.SKILLS_TEST=", 1)[1].split(";\n", 1)[0])
    pkey = s["primary_pair"]                      # skill-a_vs_skill-b == pair #3
    chart = next(c for c in blob["comparisons"] if c["key"] == pkey)
    # Primary metric CI must match BIT-for-BIT (summary uses ci_low/ci_high; the
    # HTML blob uses lo/hi for the same numbers):
    summ_p = s["comparisons"][pkey]["itt"]["tests_pass"]
    assert summ_p["ci_low"] == chart["itt"]["tests_pass"]["lo"]
    assert summ_p["ci_high"] == chart["itt"]["tests_pass"]["hi"]
    # A BH-corrected secondary metric's q-value must also match (summary key is
    # q_value, HTML blob key is q):
    summ_s = s["comparisons"][pkey]["itt"]["diff_lines"]
    assert summ_s["q_value"] == chart["itt"]["diff_lines"]["q"]


def test_compute_estimates_single_pass_is_order_stable():
    cfg = _cfg(skill_name="skill-a", skill_b_src=Path("/s/b"), skill_b_name="skill-b")
    res = _noisy_three_arm_results()
    metrics = [s.name for s in h.default_scorers(cfg)] + ["cost_usd"]
    a = h.compute_estimates(res, cfg, metrics, seed=7)
    b = h.compute_estimates(res, cfg, metrics, seed=7)
    pkey = "skill-a_vs_skill-b"
    assert a[pkey]["itt"]["tests_pass"].ci_low == b[pkey]["itt"]["tests_pass"].ci_low
    assert a[pkey]["itt"]["tests_pass"].ci_high == b[pkey]["itt"]["tests_pass"].ci_high


# --------------------------------------------------------------------------
# Engine hooks for the serve/SSE layer (Plan 021)  -- serve-1
# --------------------------------------------------------------------------

def test_summarize_agent_event_stream_yields_text_tool_result():
    """_summarize_agent_event bridges raw `claude -p` stream-json into the SSE
    `agent` events the live console renders. A realistic stream (assistant text +
    tool_use, then a final result) must summarize to ordered text -> tool -> result
    events, all carrying the cell label, with whitespace-only text dropped so the
    console isn't spammed with blank lines."""
    label = "add-email-skill_on-0"
    stream = [
        {"type": "system", "subtype": "init"},          # unknown shape -> nothing
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "Reading the file to understand the bug."},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/x.py"}},
        ]}},
        {"type": "assistant", "message": {"content": [
            {"type": "text", "text": "   "},             # whitespace-only -> dropped
        ]}},
        {"type": "result", "is_error": False, "total_cost_usd": 0.07, "num_turns": 5},
    ]
    events = [e for ev in stream for e in h._summarize_agent_event(ev, label)]

    assert [e["kind"] for e in events] == ["text", "tool", "result"]
    assert all(e["type"] == "agent" and e["label"] == label for e in events)
    txt, tool, res = events
    assert txt["text"] == "Reading the file to understand the bug."
    assert tool["tool"] == "Read"
    assert res["cost_usd"] == 0.07 and res["turns"] == 5
    # The unknown system event contributes nothing on its own.
    assert h._summarize_agent_event(stream[0], label) == []


def test_discover_runs_sorts_newest_first_and_skips_malformed():
    """discover_runs powers the Dashboard history. It must surface every dir with a
    valid summary.json newest-first (by manifest timestamp) and silently skip
    malformed/partial dirs so one half-written run can't blank the whole view."""
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        for name, ts in [("run-old", 100.0), ("run-new", 200.0)]:
            d = root / name
            d.mkdir()
            cfg = ExperimentConfig(
                repo_path=Path("."), base_ref="HEAD", skill_src=Path("demo_skill"),
                skill_name="write-tests-first", results_dir=d,
                k=6, bootstrap_iters=200, permutation_iters=200)
            man = h.experiment_manifest(cfg, timestamp=ts, offline=True)
            (d / "summary.json").write_text(
                json.dumps(h.summary_dict(h._demo_results(), cfg, man)))
        # Malformed: a summary.json that isn't valid JSON (decode-error path).
        bad = root / "run-bad"
        bad.mkdir()
        (bad / "summary.json").write_text("{ not valid json")
        # Partial: a dir with no summary.json at all (skipped at the guard).
        (root / "loose-dir").mkdir()

        cards = h.discover_runs(root)
        assert [c["id"] for c in cards] == ["run-new", "run-old"]   # newest-first
        assert all(c["skill_a"] == "write-tests-first" for c in cards)
        assert cards[0]["created_ts"] == 200.0


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
