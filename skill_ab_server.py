"""skill_ab_server — the local HTTP/SSE backend for `skill-ab serve`.

A thin orchestration layer over the engine (`skill_ab_harness`). It NEVER
re-implements statistics, scoring, or rendering — it drives `run_experiment` and
serves the engine's own `report.html` / `summary.json` / `badge.svg`.

Security posture (this server spawns paid, file-editing agents):
  * Binds 127.0.0.1 ONLY — never 0.0.0.0.
  * A random per-process session token is required on every `/api/*` route
    (`X-Skill-AB-Token` header or `?token=`); missing/wrong -> 403.
  * DNS-rebinding defense: a request whose `Host` is not 127.0.0.1/localhost
    (with our port) -> 403, applied even to `GET /` so the token can't be
    exfiltrated by a rebound origin.
  * Mutating (POST) requests with a cross-origin `Origin` header -> 403.

Subscription, not API tokens: real runs go through `run_experiment -> claude -p`
under the user's Claude Code login. There is deliberately NO reference to the
Anthropic Agent SDK or `ANTHROPIC_API_KEY` anywhere — "cost" is a usage proxy.

Stdlib only: http.server (ThreadingHTTPServer) + asyncio (engine) + threading +
queue (SSE fan-out). No framework, no external assets.
"""

from __future__ import annotations

import asyncio
import json
import queue
import re
import secrets
import shutil
import subprocess
import threading
import time
import urllib.parse
import webbrowser
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import skill_ab_harness as h

# A usage prior so the estimate UI shows *something*; it is a subscription usage
# proxy, NOT a billed dollar amount (see the note returned by /api/estimate).
DEFAULT_PER_RUN_USD = 0.05
DEFAULT_PER_RUN_SECONDS = 45.0
_USAGE_NOTE = ("estimated usage; billed against your Claude subscription's "
               "rate/usage limits, not API dollars")
_MAX_BODY = 1_000_000          # cap POST bodies; this is a local control plane
_BACKLOG_MAX = 20_000          # bounded per-run SSE backlog (memory guard)
_RUNS_MAX = 200                # cap in-memory runs; evict oldest TERMINAL (on disk anyway)
_DEFAULT_MODEL = h.ExperimentConfig.model   # the experiment default model id


def _wants_model_comparison(req: dict) -> bool:
    """Skill B left as 'none' but a different Model B picked -> compare skill_a under
    two models (mirror skill_a into arm B). Both models default to the experiment
    default, so 'default vs opus' counts as a comparison."""
    eff_a = (req.get("model_a") or _DEFAULT_MODEL)
    eff_b = (req.get("model_b") or _DEFAULT_MODEL)
    return _wants_control(req.get("skill_b")) and eff_b != eff_a
_HOSTS = ("127.0.0.1", "localhost")
_ID_RE = re.compile(r"[A-Za-z0-9._-]+")
_CONTROL_TOKENS = ("none", "off", "-", "control", "")

# Sentinel pushed onto a subscriber queue after a terminal event so the SSE loop
# knows to stop without guessing from event types.
_SENTINEL = object()


# ---------------------------------------------------------------------------
# Run registry: active runs + per-run SSE backlog and subscriber queues
# ---------------------------------------------------------------------------

@dataclass
class _RunInfo:
    run_id: str
    meta: dict
    status: str = "running"          # running | done | error | aborted
    terminal: bool = False
    n_valid: int = 0
    spent_usd: float = 0.0
    # The backlog lets a late EventSource replay from the start; bounded so a
    # huge run can't grow memory without limit.
    backlog: deque = field(default_factory=lambda: deque(maxlen=_BACKLOG_MAX))
    subscribers: set = field(default_factory=set)
    abort_event: threading.Event = field(default_factory=threading.Event)
    loop: asyncio.AbstractEventLoop | None = None


def _cancel_all_tasks(loop: asyncio.AbstractEventLoop) -> None:
    """Cancel every task on `loop` — runs INSIDE the loop thread (scheduled via
    call_soon_threadsafe) so it's safe to touch the loop's tasks."""
    for task in asyncio.all_tasks(loop):
        task.cancel()


class RunRegistry:
    """Thread-safe map of run_id -> _RunInfo. One lock guards everything; the SSE
    fan-out is a simple put onto every subscriber's queue."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, _RunInfo] = {}

    def create(self, run_id: str, meta: dict) -> None:
        with self._lock:
            self._runs[run_id] = _RunInfo(run_id=run_id, meta=dict(meta))
            # Bound memory: drop the oldest TERMINAL runs over the cap (their
            # report/summary live on disk and stay discoverable). Never evict a
            # running run. dict preserves insertion order -> oldest first.
            for rid in list(self._runs):
                if len(self._runs) <= _RUNS_MAX:
                    break
                if self._runs[rid].terminal:
                    del self._runs[rid]

    def get(self, run_id: str) -> _RunInfo | None:
        with self._lock:
            return self._runs.get(run_id)

    def set_loop(self, run_id: str, loop: asyncio.AbstractEventLoop) -> None:
        with self._lock:
            info = self._runs.get(run_id)
            if info is not None:
                info.loop = loop

    def clear_loop(self, run_id: str) -> None:
        with self._lock:
            info = self._runs.get(run_id)
            if info is not None:
                info.loop = None

    def publish(self, run_id: str, evt: dict) -> None:
        """Append to the backlog and fan out to every subscriber. A terminal
        event (experiment_done/error) also pushes the sentinel so streams close.
        Events after a terminal are dropped (idempotent termination)."""
        with self._lock:
            info = self._runs.get(run_id)
            if info is None or info.terminal:
                return
            self._record(info, evt)
            terminal = evt.get("type") in ("experiment_done", "error")
            if terminal:
                info.terminal = True
                info.status = "done" if evt["type"] == "experiment_done" else "error"
            info.backlog.append(evt)
            for q in info.subscribers:
                q.put(evt)
                if terminal:
                    q.put(_SENTINEL)

    @staticmethod
    def _record(info: _RunInfo, evt: dict) -> None:
        """Keep the live RunCard fields (cost ticker, valid count) current."""
        t = evt.get("type")
        if t == "cost" and evt.get("spent_usd") is not None:
            info.spent_usd = evt["spent_usd"]
        elif t == "run_done" and evt.get("itt_valid"):
            info.n_valid += 1

    def terminate(self, run_id: str, status: str, evt: dict) -> bool:
        """Force a terminal state (used by abort). Returns False if already
        terminal so a double abort / late finish can't fire two terminals."""
        with self._lock:
            info = self._runs.get(run_id)
            if info is None or info.terminal:
                return False
            info.terminal = True
            info.status = status
            info.backlog.append(evt)
            for q in info.subscribers:
                q.put(evt)
                q.put(_SENTINEL)
            return True

    def subscribe(self, run_id: str) -> queue.Queue | None:
        """Return a queue preloaded with the backlog (replay) and registered for
        future events. Done atomically under the lock with publish(), so a
        concurrent event is never missed nor duplicated."""
        with self._lock:
            info = self._runs.get(run_id)
            if info is None:
                return None
            q: queue.Queue = queue.Queue()
            for evt in list(info.backlog):
                q.put(evt)
            if info.terminal:
                q.put(_SENTINEL)
            info.subscribers.add(q)
            return q

    def unsubscribe(self, run_id: str, q: queue.Queue) -> None:
        with self._lock:
            info = self._runs.get(run_id)
            if info is not None:
                info.subscribers.discard(q)

    def abort(self, run_id: str) -> bool:
        """Best-effort cancel: set the abort flag (demo loops honor it), cancel
        the engine's asyncio tasks (stops scheduling new cells), and force an
        'aborted' terminal so the SSE stream closes. In-flight `claude -p`
        subprocesses may still finish — true kill needs an engine hook."""
        with self._lock:
            info = self._runs.get(run_id)
            if info is None or info.terminal:
                return False
            info.abort_event.set()
            loop = info.loop
        if loop is not None:
            try:
                loop.call_soon_threadsafe(_cancel_all_tasks, loop)
            except RuntimeError:
                pass                           # run finished + closed its loop mid-abort
        return self.terminate(run_id, "aborted",
                              {"type": "error", "message": "run aborted by user"})

    def snapshot_cards(self) -> list[dict]:
        """RunCard dicts for the in-memory (possibly still-running) runs."""
        with self._lock:
            return [_card_from_info(info) for info in self._runs.values()]


def _card_from_info(info: _RunInfo) -> dict:
    m = info.meta
    return {
        "id": info.run_id, "title": m.get("title"),
        "skill_a": m.get("skill_a"), "skill_b": m.get("skill_b"),
        "target": m.get("target"), "status": info.status, "verdict": None,
        "primary_metric": m.get("primary_metric", "tests_pass"),
        "created_ts": m.get("created_ts"),
        "cost_usd": info.spent_usd or None, "n_valid": info.n_valid,
        "report_url": None, "badge_url": None, "demo": bool(m.get("demo")),
    }


# ---------------------------------------------------------------------------
# Building cfg + tasks from a request (reuses the quick path's resolvers)
# ---------------------------------------------------------------------------

def _wants_control(skill_b) -> bool:
    return not skill_b or str(skill_b).lower() in _CONTROL_TOKENS


# Runners the loopback UI may select. The browser may pick a preset NAME only; the
# actual argv lives in h._RUNNERS (hardcoded in the engine), so a raw command template
# is NEVER honored from a request body -- that would be loopback RCE (plan 022 blocker
# #5). The UI wires only arm B to an alternate CLI; arm A is always claude+skill and the
# control is always claude, which keeps the surface minimal.
_RUNNER_CLAUDE_TOKENS = frozenset({"", "claude", "default", "none"})


def _available_runner_presets() -> list[dict]:
    """The runner choices the UI may offer: built-in claude + every curated engine
    preset. Driven by h._RUNNERS so the UI can't drift from what the server accepts."""
    return [{"id": "claude", "label": "Claude (default)"}] + [
        {"id": name, "label": spec.get("label", name)}
        for name, spec in sorted(h._RUNNERS.items())]


def _resolve_runner_preset(value) -> str | None:
    """Map a request runner value to a SAFE runner: None (built-in claude) or a curated
    preset NAME in h._RUNNERS. Anything else -- crucially a raw command template -- is
    REJECTED, never passed through. This is the loopback-RCE guard for the web UI."""
    if value is None:
        return None
    name = str(value).strip().lower()
    if name in _RUNNER_CLAUDE_TOKENS:
        return None
    if name in h._RUNNERS:
        return name
    raise ValueError(
        f"unknown runner preset {value!r}: the web UI may only select a curated preset "
        f"({', '.join(sorted(h._RUNNERS)) or 'none available'}), never a raw command")


def _build_run_config(req: dict, run_dir: Path) -> tuple[h.ExperimentConfig, list[h.Task]]:
    """Resolve a real StartReq into (cfg, tasks). Mirrors `_build_quick`: a PR
    target gets gh/push denied so no run mutates the shared PR; '.'/branch
    targets need an (optional, non-frozen) `prompt`. Raises on bad input."""
    skill_a = req.get("skill_a")
    if not skill_a:
        raise ValueError("skill_a is required")
    md_a = h.resolve_skill(skill_a)
    # Arm B may be an alternate agent CLI (curated preset only; raw commands rejected).
    runner_b = _resolve_runner_preset(req.get("runner_b"))
    skill_b = req.get("skill_b")
    skill_b_src = skill_b_name = None
    if runner_b is not None:
        pass                                        # external CLI arm: no skill, no model
    elif not _wants_control(skill_b):
        md_b = h.resolve_skill(skill_b)             # explicit 2nd skill (head-to-head)
        skill_b_src, skill_b_name = md_b.parent, skill_b
    elif _wants_model_comparison(req):
        skill_b_src, skill_b_name = md_a.parent, skill_a   # same skill, two models
    target = req.get("target") or "."
    repo, base_ref, task_prompt, setup, test = h.resolve_target(target, req.get("prompt"))
    deny = ("Bash(gh:*)", "Bash(git push:*)") if h._PR_RE.search(target) else ()
    isolation = req.get("isolation") if req.get("isolation") in ("inject", "worktree") else "inject"
    # SECURITY (plan 022, blocker #5): the loopback UI may pick a curated runner PRESET
    # NAME for arm B only (resolved above via _resolve_runner_preset against h._RUNNERS) --
    # never a raw command template, which would be remote code execution over loopback.
    # runner_a/runner_off are never read at all: arm A is always claude+skill and the
    # control is always claude. Raw command templates remain TOML/CLI-only.
    cfg = h.ExperimentConfig(
        repo_path=repo, base_ref=base_ref,
        skill_src=md_a.parent, skill_name=skill_a,
        skill_b_src=skill_b_src, skill_b_name=skill_b_name, runner_b=runner_b,
        model_a=req.get("model_a") or None,
        # a codex arm has no claude model -> don't attach one (keeps the arm label clean)
        model_b=None if runner_b else (req.get("model_b") or None),
        include_control=bool(req.get("include_control", True)),
        k=max(1, min(20, int(req.get("k") or 3))),   # clamp: UI caps 1-10; never runaway
        judge_enabled=bool(req.get("judge")),
        disallowed_tools=deny, isolation=isolation, results_dir=run_dir)
    task = h.Task(id="task", prompt=task_prompt, setup_cmd=setup, test_cmd=test)
    return cfg, [task]


def _build_estimate_config(req: dict) -> tuple[h.ExperimentConfig, list[h.Task]]:
    """A LIGHTWEIGHT cfg just for `estimate_cost` (it only needs arm count, k and
    judge). Skill resolution is best-effort: a cost projection must work BEFORE a
    skill is installed (and in tests), since the run count depends only on the arm
    count + k, not on the skill files existing on disk."""
    skill_a = req.get("skill_a")
    if not skill_a:
        raise ValueError("skill_a is required")
    runner_b = _resolve_runner_preset(req.get("runner_b"))
    skill_b = req.get("skill_b")
    skill_b_src = skill_b_name = None
    if runner_b is not None:
        pass                                          # external-CLI arm B = 2nd arm
    elif not _wants_control(skill_b):
        # Dummy non-None src just to make this a head-to-head for the count.
        skill_b_src, skill_b_name = Path("."), skill_b
    elif _wants_model_comparison(req):                # same skill, two models = 2 arms
        skill_b_src, skill_b_name = Path("."), skill_a
    cfg = h.ExperimentConfig(
        repo_path=Path("."), base_ref="HEAD", skill_src=Path("."),
        skill_name=skill_a, skill_b_src=skill_b_src, skill_b_name=skill_b_name,
        runner_b=runner_b,
        model_a=req.get("model_a") or None,
        model_b=None if runner_b else (req.get("model_b") or None),
        include_control=bool(req.get("include_control", True)),
        k=max(1, min(20, int(req.get("k") or 3))),   # clamp: UI caps 1-10; never runaway
        judge_enabled=bool(req.get("judge")))
    return cfg, [h.Task(id="task", prompt="estimate")]


# ---------------------------------------------------------------------------
# Run launchers (background threads). on_event publishes into the registry.
# ---------------------------------------------------------------------------

def _summary_card(summary: dict, verdict: dict | None) -> dict:
    """Compact card for the live UI's `experiment_done` (no need to re-fetch)."""
    return {
        "verdict": verdict["label"] if verdict else None,
        "primary_metric": summary.get("primary_metric"),
        "point": verdict["point"] if verdict else None,
        "ci_low": verdict["ci_low"] if verdict else None,
        "ci_high": verdict["ci_high"] if verdict else None,
        "n_tasks": verdict["n_tasks"] if verdict else None,
        "on_valid": (summary.get("validity") or {}).get("on_valid"),
        "off_valid": (summary.get("validity") or {}).get("off_valid"),
    }


def _finalize_run(server, run_id: str, cfg: h.ExperimentConfig,
                  results: list, pf, demo: bool) -> None:
    """Write report.html + summary.json + badge.svg into the run dir, persist
    meta.json (so demo/title survive a restart), then publish experiment_done."""
    run_dir = cfg.results_dir
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = h.experiment_manifest(cfg, seed=0, timestamp=time.time(), offline=demo)
    summary = h.summary_dict(results, cfg, manifest)
    (run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    (run_dir / "report.html").write_text(h.build_html_report(results, pf, cfg, manifest))
    verdict = h.primary_verdict(summary, cfg)
    badge_url = None
    if verdict:
        (run_dir / "badge.svg").write_text(
            h.render_badge_svg(summary["primary_metric"], verdict))
        badge_url = f"/api/runs/{run_id}/badge"
    info = server.registry.get(run_id)
    if info is not None:
        try:
            (run_dir / "meta.json").write_text(json.dumps(info.meta, indent=2))
        except OSError:
            pass
    server.registry.publish(run_id, {
        "type": "experiment_done", "run_id": run_id,
        "verdict": verdict["label"] if verdict else None,
        "primary_metric": summary["primary_metric"],
        "report_url": f"/api/runs/{run_id}/report", "badge_url": badge_url,
        "summary_card": _summary_card(summary, verdict)})


def _run_real(server, run_id: str, cfg: h.ExperimentConfig, tasks: list) -> None:
    """Own the event loop (so abort can cancel its tasks) and run the engine. A
    final terminal event is ALWAYS published so the SSE stream never hangs."""
    registry = server.registry
    loop = asyncio.new_event_loop()
    registry.set_loop(run_id, loop)
    try:
        asyncio.set_event_loop(loop)
        results, pf = loop.run_until_complete(
            h.run_experiment(cfg, tasks, on_event=lambda e: registry.publish(run_id, e)))
        _finalize_run(server, run_id, cfg, results, pf, demo=False)
    except asyncio.CancelledError:
        pass                                       # aborted; registry already terminal
    except (Exception, SystemExit) as exc:         # noqa: BLE001 — surface, never crash
        registry.terminate(run_id, "error",
                           {"type": "error", "message": f"{type(exc).__name__}: {exc}"})
    finally:
        registry.clear_loop(run_id)
        try:
            loop.close()
        except Exception:                          # noqa: BLE001
            pass


def _demo_cfg(run_dir: Path) -> h.ExperimentConfig:
    return h.ExperimentConfig(
        repo_path=Path("."), base_ref="HEAD", skill_src=Path("demo_skill"),
        skill_name="write-tests-first", results_dir=run_dir, k=6)


def _demo_agent_lines(r) -> list[str]:
    """Synthetic console text derived from the stored diff (truncated)."""
    name = "write-tests-first" if r.arm is h.Arm.SKILL_ON else "control"
    lines = [f"[{name}] working on {r.task_id}…"]
    added = [ln for ln in (r.diff or "").splitlines()
             if ln.startswith("+") and not ln.startswith("+++")]
    for a in added[:2]:
        lines.append("edit: " + a[1:].strip()[:80])
    return lines


def _run_demo(server, run_id: str) -> None:
    """Replay the bundled demo as a timed event sequence — ZERO claude, ZERO
    spend. The small sleeps make the live console visibly stream."""
    registry = server.registry
    info = registry.get(run_id)
    abort = info.abort_event if info else threading.Event()
    cfg = _demo_cfg(server.runs_dir / run_id)
    results = h._demo_results()
    arms = [h.arm_label(cfg, a) for a in h.experiment_arms(cfg)]
    cells = [{"label": f"{r.task_id}-{r.arm.value}-{r.run_index}", "task": r.task_id,
              "arm": h.arm_label(cfg, r.arm), "idx": r.run_index} for r in results]
    registry.publish(run_id, {"type": "experiment_start", "arms": arms, "cells": cells})
    spent = 0.0
    try:
        for r in results:
            if abort.is_set():
                return                             # abort() already published terminal
            label = f"{r.task_id}-{r.arm.value}-{r.run_index}"
            registry.publish(run_id, {"type": "run_start", "label": label})
            time.sleep(0.12)
            for line in _demo_agent_lines(r):
                if abort.is_set():
                    return
                registry.publish(run_id, {"type": "agent", "label": label,
                                          "kind": "text", "text": line})
                time.sleep(0.12)
            registry.publish(run_id, {
                "type": "run_done", "label": label, "itt_valid": r.itt_valid,
                "activated": r.skill_activated, "contaminated_by": r.contaminated_by,
                "cost_usd": r.cost_usd, "turns": r.num_turns,
                "diff_lines": r.scores.get("diff_lines")})
            spent += r.cost_usd or 0.0
            registry.publish(run_id, {"type": "cost", "spent_usd": round(spent, 3),
                                      "ceiling_usd": None})
            time.sleep(0.1)
        _finalize_run(server, run_id, cfg, results, h.Preflight(), demo=True)
    except (Exception, SystemExit) as exc:         # noqa: BLE001
        registry.terminate(run_id, "error",
                           {"type": "error", "message": f"{type(exc).__name__}: {exc}"})


def _new_run_id() -> str:
    # Sortable + unique + filesystem-safe (only [A-Za-z0-9-]).
    return f"{time.strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(3)}"


# ---------------------------------------------------------------------------
# History listing (engine's discover_runs, enriched with title + demo flag)
# ---------------------------------------------------------------------------

def _read_meta(run_dir: Path) -> dict:
    try:
        return json.loads((run_dir / "meta.json").read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def _derive_title(card: dict) -> str:
    return f"{card.get('skill_a')} vs {card.get('skill_b') or 'control'}"


def _list_run_cards(runs_dir: Path, registry: RunRegistry) -> list[dict]:
    cards = h.discover_runs(runs_dir)
    seen = set()
    for c in cards:
        meta = _read_meta(runs_dir / c["id"])
        c.setdefault("title", meta.get("title") or _derive_title(c))
        c["demo"] = bool(meta.get("demo"))
        seen.add(c["id"])
    # Surface in-memory runs not yet on disk (running / just-failed) so the
    # dashboard updates live without waiting for summary.json.
    for card in registry.snapshot_cards():
        if card["id"] not in seen:
            cards.append(card)
    cards.sort(key=lambda r: (r.get("created_ts") or 0), reverse=True)
    return cards


def _gallery_entries(runs_dir: Path) -> list[dict]:
    entries = []
    if not runs_dir.exists():
        return entries
    for d in sorted(runs_dir.iterdir()):
        sp = d / "summary.json"
        if not (d.is_dir() and sp.exists()):
            continue
        try:
            s = json.loads(sp.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        href = f"/api/runs/{d.name}/report" if (d / "report.html").exists() else None
        entries.append({"summary": s, "report_href": href})
    return entries


# ---------------------------------------------------------------------------
# App shell (frontend lives in skill_ab_app; lazy import keeps this testable)
# ---------------------------------------------------------------------------

def _fallback_shell(token: str) -> str:
    """Minimal shell if skill_ab_app isn't importable (parallel build / error).
    Still embeds the token so the page can reach the API."""
    safe = json.dumps(token)
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<title>skill-ab</title></head><body>"
        "<h1>skill-ab serve</h1>"
        "<p>Frontend module unavailable; the API is up.</p>"
        f"<script>window.SKILL_AB_TOKEN={safe};</script>"
        "</body></html>")


def _app_html(token: str) -> str:
    try:
        from skill_ab_app import app_shell_html
        return app_shell_html(token)
    except Exception:                              # noqa: BLE001 — never 500 on the shell
        return _fallback_shell(token)


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"     # keep-alive (we send Content-Length); SSE opts out

    # -- security middleware -------------------------------------------------

    def _port(self) -> int:
        return self.server.server_address[1]

    @staticmethod
    def _netloc_ok(netloc: str, port: int) -> bool:
        name = netloc.rsplit(":", 1)[0] if netloc else ""
        has_port = ":" in netloc
        port_part = netloc.rsplit(":", 1)[1] if has_port else None
        if name not in _HOSTS:
            return False
        return not (port_part is not None and port_part != str(port))

    def _check_host(self) -> bool:
        return self._netloc_ok(self.headers.get("Host", ""), self._port())

    def _check_token(self) -> bool:
        tok = self.headers.get("X-Skill-AB-Token")
        if tok is None:
            qs = urllib.parse.urlparse(self.path).query
            tok = urllib.parse.parse_qs(qs).get("token", [None])[0]
        # A non-ASCII value makes compare_digest raise TypeError; reject it as a clean
        # 403 (the real token is url-safe ASCII) instead of leaking a 500.
        if tok is None or not tok.isascii():
            return False
        # constant-time compare so a wrong token can't be timing-probed
        return secrets.compare_digest(tok, self.server.token)

    def _check_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True                            # non-browser / same-origin
        return self._netloc_ok(urllib.parse.urlparse(origin).netloc, self._port())

    def _authed(self) -> bool:
        """Host + token gate for every /api/* route. Sends the 403 itself."""
        if not self._check_host():
            self._send_json({"error": "bad host"}, 403)
            return False
        if not self._check_token():
            self._send_json({"error": "forbidden"}, 403)
            return False
        return True

    # -- response helpers ----------------------------------------------------

    def _send_bytes(self, body: bytes, ctype: str, status: int = 200,
                    headers: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        # Defense-in-depth for the session token: no-referrer stops the token (in
        # ?token= URLs / the iframed report) leaking via the Referer header; DENY
        # stops a foreign page framing the token-bearing control plane (clickjacking).
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        # The shell embeds the session token; keep token-bearing responses out of the
        # browser disk cache (also avoids serving a stale UI after an upgrade).
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, obj: dict, status: int = 200) -> None:
        self._send_bytes(json.dumps(obj).encode(), "application/json", status)

    def _send_text(self, text: str, ctype: str, status: int = 200) -> None:
        self._send_bytes(text.encode("utf-8"), ctype, status)

    def _send_file(self, path: Path, ctype: str) -> None:
        try:
            self._send_bytes(path.read_bytes(), ctype)
        except OSError:
            self._send_json({"error": "not found"}, 404)

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True       # malformed header -> don't read a body
            return {}
        if length > _MAX_BODY:
            self.close_connection = True           # avoid desync on a discarded body
            raise ValueError("request body too large")
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8") or "{}")

    def log_message(self, *_args) -> None:         # keep the console quiet
        pass

    # -- routing -------------------------------------------------------------

    def do_GET(self) -> None:                      # noqa: N802 (stdlib API)
        try:
            path = urllib.parse.urlparse(self.path).path
            if path == "/":
                if not self._check_host():         # protect the embedded token
                    return self._send_json({"error": "bad host"}, 403)
                return self._send_text(_app_html(self.server.token), "text/html; charset=utf-8")
            if not path.startswith("/api/"):
                return self._send_json({"error": "not found"}, 404)
            if not self._authed():
                return
            self._route_get(path)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:                   # noqa: BLE001 — never crash the thread
            self._safe_500(exc)

    def do_POST(self) -> None:                     # noqa: N802 (stdlib API)
        try:
            # Read the body FIRST (bounded) so an auth-rejected POST leaves the
            # keep-alive connection in sync.
            try:
                body = self._read_body()
            except ValueError as exc:
                return self._send_json({"error": str(exc)}, 413)
            except json.JSONDecodeError:
                return self._send_json({"error": "invalid JSON body"}, 400)
            path = urllib.parse.urlparse(self.path).path
            if not self._authed():
                return
            if not self._check_origin():
                return self._send_json({"error": "cross-origin"}, 403)
            self._route_post(path, body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:                   # noqa: BLE001
            self._safe_500(exc)

    def _route_get(self, path: str) -> None:
        if path == "/api/health":
            return self._handle_health()
        if path == "/api/skills":
            # The installed skills a typed name would resolve to (for the picker), plus
            # the curated agent-CLI presets arm B may use (claude + codex/...).
            return self._send_json({
                "skills": h.list_available_skills(project_dir=Path(".")),
                "runners": _available_runner_presets()})
        if path == "/api/runs":
            return self._send_json({"runs": _list_run_cards(self.server.runs_dir,
                                                            self.server.registry)})
        if path == "/api/gallery":
            return self._send_text(
                h.build_gallery_html(_gallery_entries(self.server.runs_dir)),
                "text/html; charset=utf-8")
        run_id, sub = _parse_run_subpath(path)
        if run_id is None:
            return self._send_json({"error": "not found"}, 404)
        return self._handle_run_get(run_id, sub)

    def _route_post(self, path: str, body: dict) -> None:
        if path == "/api/estimate":
            return self._handle_estimate(body)
        if path == "/api/runs":
            return self._handle_start(body)
        run_id, sub = _parse_run_subpath(path)
        if run_id is not None and sub == "abort":
            return self._handle_abort(run_id)
        self._send_json({"error": "not found"}, 404)

    # -- handlers ------------------------------------------------------------

    def _handle_health(self) -> None:
        claude = shutil.which("claude")
        version = None
        if claude:
            try:
                version = subprocess.run(
                    ["claude", "--version"], capture_output=True, text=True,
                    timeout=10).stdout.strip() or None
            except (OSError, subprocess.SubprocessError):
                version = None
        self._send_json({
            "ok": True, "claude_on_path": bool(claude), "claude_version": version,
            "runs_dir": str(self.server.runs_dir), "harness_version": h.__version__,
            "model": h.ExperimentConfig.model})

    def _handle_run_get(self, run_id: str, sub: str | None) -> None:
        run_dir = self.server.runs_dir / run_id
        if sub == "events":
            return self._stream_events(run_id)
        if sub == "summary":
            return self._send_file(run_dir / "summary.json", "application/json")
        if sub == "report":
            return self._send_file(run_dir / "report.html", "text/html; charset=utf-8")
        if sub == "badge":
            return self._send_file(run_dir / "badge.svg", "image/svg+xml")
        self._send_json({"error": "not found"}, 404)

    def _handle_estimate(self, body: dict) -> None:
        try:
            cfg, tasks = _build_estimate_config(body)
        except (Exception, SystemExit) as exc:     # noqa: BLE001 — return, don't 500
            return self._send_json({"error": f"{type(exc).__name__}: {exc}"})
        est = h.estimate_cost(cfg, tasks, per_run_usd=DEFAULT_PER_RUN_USD,
                              per_run_seconds=DEFAULT_PER_RUN_SECONDS)
        self._send_json({
            "n_runs": est["n_runs"], "n_judge_calls": est["n_judge_calls"],
            "projected_usd": est["projected_usd"],
            "projected_wall_seconds": est["projected_wall_seconds"],
            "note": _USAGE_NOTE})

    def _handle_start(self, body: dict) -> None:
        run_id = _new_run_id()
        registry = self.server.registry
        if body.get("demo"):
            registry.create(run_id, {
                "demo": True, "skill_a": "write-tests-first", "skill_b": None,
                "target": "demo", "title": "write-tests-first (demo)",
                "primary_metric": "tests_pass", "created_ts": time.time()})
            threading.Thread(target=_run_demo, args=(self.server, run_id),
                             daemon=True).start()
            return self._send_json({"run_id": run_id}, 202)
        try:
            cfg, tasks = _build_run_config(body, self.server.runs_dir / run_id)
        except (Exception, SystemExit) as exc:     # noqa: BLE001
            return self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 400)
        registry.create(run_id, {
            "demo": False, "skill_a": cfg.skill_name, "skill_b": cfg.skill_b_name,
            "target": body.get("target") or ".",
            "title": cfg.skill_name + (f" vs {cfg.skill_b_name}" if cfg.skill_b_name
                                       else " vs control"),
            "primary_metric": cfg.primary_metric, "created_ts": time.time()})
        threading.Thread(target=_run_real, args=(self.server, run_id, cfg, tasks),
                         daemon=True).start()
        self._send_json({"run_id": run_id}, 202)

    def _handle_abort(self, run_id: str) -> None:
        if self.server.registry.get(run_id) is None:
            return self._send_json({"error": "no such run"}, 404)
        self._send_json({"aborted": self.server.registry.abort(run_id)})

    def _stream_events(self, run_id: str) -> None:
        """SSE: replay the backlog then live-stream until a terminal event. A
        client disconnect (BrokenPipe) unsubscribes cleanly."""
        q = self.server.registry.subscribe(run_id)
        if q is None:
            return self._send_json({"error": "no such run"}, 404)
        self.close_connection = True               # we stream until close, no keep-alive
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")   # defeat any proxy buffering
        self.end_headers()
        try:
            while True:
                try:
                    item = q.get(timeout=15)
                except queue.Empty:
                    self.wfile.write(b": keepalive\n\n")   # heartbeat detects disconnect
                    self.wfile.flush()
                    continue
                if item is _SENTINEL:
                    break
                self.wfile.write(f"data: {json.dumps(item)}\n\n".encode())
                self.wfile.flush()
                if item.get("type") in ("experiment_done", "error"):
                    break
        except (BrokenPipeError, ConnectionResetError, ValueError):
            pass
        finally:
            self.server.registry.unsubscribe(run_id, q)

    def _safe_500(self, exc: Exception) -> None:
        try:
            self._send_json({"error": f"{type(exc).__name__}: {exc}"}, 500)
        except Exception:                          # noqa: BLE001 — headers may be sent
            pass


def _parse_run_subpath(path: str) -> tuple[str | None, str | None]:
    """`/api/runs/<id>/<sub>` -> (id, sub); validate id (no path traversal)."""
    parts = path.split("/")
    if len(parts) != 5 or parts[1] != "api" or parts[2] != "runs":
        return None, None
    run_id = parts[3]
    if ".." in run_id or _ID_RE.fullmatch(run_id) is None:
        return None, None
    return run_id, parts[4]


# ---------------------------------------------------------------------------
# Server construction + entry point
# ---------------------------------------------------------------------------

class _Server(ThreadingHTTPServer):
    daemon_threads = True            # don't let request threads block shutdown
    allow_reuse_address = True

    def __init__(self, addr, handler, *, runs_dir: Path, token: str,
                 registry: RunRegistry) -> None:
        super().__init__(addr, handler)
        self.runs_dir = runs_dir
        self.token = token
        self.registry = registry


def make_server(runs_dir, token, host: str = "127.0.0.1", port: int = 7878) -> _Server:
    """Build (don't start) the server. Tests serve_forever() in a thread then
    .shutdown(). A None token is generated here. Binds 127.0.0.1 only."""
    runs_dir = Path(runs_dir)
    runs_dir.mkdir(parents=True, exist_ok=True)
    if token is None:
        token = secrets.token_urlsafe(32)
    return _Server((host, port), _Handler, runs_dir=runs_dir, token=token,
                   registry=RunRegistry())


def serve(host: str = "127.0.0.1", port: int = 7878,
          runs_dir: Path = Path.home() / ".skill-ab" / "runs",
          open_browser: bool = True, token: str | None = None) -> None:
    """Start the local app: build the server, print the tokenized URL, optionally
    open a browser, then serve until Ctrl-C."""
    if token is None:
        token = secrets.token_urlsafe(32)
    srv = make_server(runs_dir, token, host, port)
    real_port = srv.server_address[1]
    url = f"http://127.0.0.1:{real_port}/?token={token}"
    print(f"skill-ab is live at {url}")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:                          # noqa: BLE001 — headless is fine
            pass
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()
        srv.server_close()
