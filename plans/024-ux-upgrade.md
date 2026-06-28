# Plan 024: UX upgrade — interactive diffs, treatment panel, guided flows, visual polish

> Authored from a 9-agent design workflow (4 UX audits → judged 3-way diff-viewer panel → synthesis), 2026-06-28, against commit `d054850`. Winner: **GitHub Split** diff viewer + grafts. Execute the roadmap (§5) in batches; the diff viewer + treatment panel are the user's explicit priorities. All work stays stdlib-only, one offline self-contained HTML, escaped server-side, both themes.

# skill-ab UX Upgrade — Build Spec + Roadmap

Single consolidated spec. The diff viewer is the winning **GitHub Split** design with the judge-approved grafts folded in (contrast band, divergence map, beat captions, diff-of-diffs mode, canvas minimap, mandatory reduced-motion gate). All work stays stdlib-only, one self-contained offline HTML file, escaped-server-side, both themes, no telemetry. Source anchors verified against the live files (`skill_ab_harness.py` 213 KB, `skill_ab_app.py`, `skill_ab_server.py`).

---

## 1. The interactive diff viewer (centerpiece)

Replaces the flat `_diff_to_html` (`skill_ab_harness.py:2307`) + independent-columns `_work_products_html` (`:3585`) with a parsed, line-numbered, file-navigable, word-highlighted diff UI that makes arm-vs-arm comparison the primary object. The diff text **never** enters `window.SKILL_AB` (built at `:3711`) and **never** reaches `innerHTML` of raw text. All markup is emitted server-side into `server_detail`, appended after `#app` (`:3706`–`3711`), so `mount()` (which only rewrites `#app.innerHTML`) never clobbers it and the serve SPA inherits it for free via the Results iframe (`skill_ab_app.py:1157`).

### 1.1 Server-side parse model (new code in `skill_ab_harness.py`)

Add a pure parser + renderer. No regex on the *content*, only on diff control lines.

```
_PatchFile  = {path, old_path, status('A'|'M'|'D'|'R'), add_count, del_count,
               hunks:[_Hunk], file_id}
_Hunk       = {header_text, old_start, new_start, rows:[_Row], hunk_id}
_Row        = {kind('ctx'|'add'|'del'|'meta'), old_n|None, new_n|None,
               segments:[(text, changed:bool)], pair_idx|None, side('o'|'n'|'b')}
```

New functions, all stdlib (`re`, `html`, `difflib`):

- **`parse_unified_diff(diff: str) -> list[_PatchFile]`** — split on `diff --git a/(\S+) b/(\S+)`; classify `new file`/`deleted file`/`rename from|to` for `status`; seed counters from each `@@ -a,b +c,d @@`; increment `old_n` on ctx/del, `new_n` on ctx/add. `+++`/`---`/`index` lines become file metadata, **not** `ctx` rows (fixes the `:2312`–`2318` fall-through bug). Count `add`/`del` per file for the `+N/−M` badge.
- **`word_diff_pairs(hunk)`** — within a hunk, pair the i-th del-run line with the i-th add-run line via `difflib.SequenceMatcher` over the *line lists*. For each (del,add) pair: if `SequenceMatcher(None, d, a).ratio() >= 0.3` **and** `max(len(d),len(a)) <= 400` (per-line cap — graft from Blink, avoids O(n·m) stalls on minified lines), run char-level `get_opcodes()`; build `segments` where each slice is `html.escape(text)` **individually** and changed slices carry `changed=True`. Below the ratio gate or over the cap → one segment, `changed=False` (whole-line tint). Assign `pair_idx` so the client can build Split without re-diffing.
- **`fold_context(rows, ctx=3, min_run=6)`** — collapse runs of ≥6 consecutive ctx rows into a `fold` marker carrying `n` hidden + the 3 lead/3 trail rows kept visible.
- **`_render_patch(patch_files, patch_id, truncated: bool) -> str`** — emits the escaped DOM (below). When `truncated`, append a visible `<div class=trunc>` row. Truncation is known because `result.diff = d.stdout[:cfg.judge_max_diff_chars]` (`:1220`) — store a sibling boolean `diff_truncated = len(d.stdout) >= cfg.judge_max_diff_chars` on `RunResult` so the marker is honest, not guessed.

### 1.2 Emitted DOM (every text node already `html.escape`'d)

```html
<div class="cmp" data-task="t1">
  <!-- CONTRAST BAND (graft: Divergence) -->
  <div class="contrast">
    <div class="scorecard" data-arm="off"> tests/lint/build pills · files · +A/−D · turns · $cost </div>
    <div class="thesis">templated one-liner</div>
    <div class="scorecard" data-arm="on"> … </div>
    <div class="divmap">  <!-- divergence map: clickable file chips -->
      <button class="chip both"       data-target="f-1-0">parser.py</button>
      <button class="chip skill-only" data-target="f-1-1">test_parser.py</button>
      <button class="chip ctrl-only"  data-target="f-1-2">README.md</button>
    </div>
  </div>

  <!-- TOOLBAR -->
  <div class="cmp-bar">
    <div class="pairsel">…</div>                 <!-- 3-arm: experiment_pairs select -->
    <div class="armrun"><select data-arm="off">run…</select>
                        <select data-arm="on">run…</select></div>
    <div class="filerail">…built by JS from data-* …</div>
    <div class="modesw" role="tablist">Compare · Focus · Diff-of-diffs</div>
    <input class="diffsearch" type="search" placeholder="search diffs (/)">
    <button data-act="unified">Unified</button><button data-act="split">Split</button>
    <button data-act="wrap">Wrap</button>
  </div>

  <!-- PANES (Compare = two; Focus = one full-width) -->
  <div class="panes" data-mode="compare">
    <div class="pane" data-arm="off" data-task="t1">  <!-- DiffView -->
      <section class="file" id="f-1-0" data-path="parser.py" data-add="12" data-del="3" data-status="M">
        <div class="file-head"><span class="caret"></span><span class="fpath">parser.py</span>
             <span class="fstat" data-tip="modified">M</span><span class="fcount">+12 −3</span>
             <button class="copy" data-scope="file">Copy</button></div>
        <div class="hunk" id="h-1-0-0">
          <div class="hunk-head" data-tip="lines 40–58 → 41–60">@@ -40,9 +41,9 @@</div>
          <div class="row ctx" data-k="0" data-of="40" data-nf="41" data-pair="0:0">
            <span class="ln">40</span><span class="ln">41</span><span class="sg"> </span>
            <span class="tx">def parse(</span></div>
          <div class="row del" data-k="2" data-of="41" data-pair="0:1" data-side="o">
            <span class="ln">41</span><span class="ln"></span><span class="sg">-</span>
            <span class="tx">  return <span class="w">old</span>(x)</span></div>
          <div class="row add" data-k="1" data-nf="42" data-pair="0:1" data-side="n">
            <span class="ln"></span><span class="ln">42</span><span class="sg">+</span>
            <span class="tx">  return <span class="w">new</span>(x)</span></div>
          <div class="fold" data-n="14"><button>Expand 14 unchanged lines</button></div>
        </div>
      </section>
    </div>
    <div class="pane" data-arm="on" data-task="t1"> … </div>
    <canvas class="minimap" data-task="t1" width="14"></canvas> <!-- graft: Blink -->
  </div>
</div>
```

`data-k` row code: `0 ctx / 1 add / 2 del / 3 divergence` — integers only, the minimap reads these (never text), keeping it escape-safe.

### 1.3 Data shape from Python (what the blob carries vs. what it doesn't)

The diff DOM is pre-rendered; the JSON blob (`:3711`) stays free-text-free. It gains only **numeric/id/flag** keys per task so JS can wire behavior without parsing:

```json
"work": {
  "t1": {
    "files":[{"id":"f-1-0","add":12,"del":3,"status":"M","diverge":"both"}],
    "arms":{"off":{"run_ids":["off-0","off-1"],"rep":"off-0"},
            "on":{"run_ids":["on-0"],"rep":"on-0"}},
    "scores":{"off":{"tests":0,"lint":1,"build":1,"files":2,"add":5,"del":1,"turns":7,"cost":0.04},
              "on":{...}},
    "truncated":{"off-0":false,"on-0":true},
    "diffofdiffs":{"parser.py":{"agree":4,"a_only":2,"b_only":1}}
  }
}
```

`diverge` (`both`/`skill-only`/`ctrl-only`) is computed server-side by set-comparing the two arms' touched paths. `diffofdiffs` per shared file = `difflib.SequenceMatcher` over arm-A vs arm-B **signed change-lines** (`equal`→agree, `delete/replace`→A-only, `insert`→B-only) — labelled a deterministic heuristic, not a 3-way merge.

### 1.4 Escaping strategy (load-bearing)

1. Every code cell = `html.escape(text)`. Word spans escape **each `get_opcodes()` segment before** wrapping in the literal `<span class="w">` — no user byte is ever interpolated unescaped.
2. JS never assigns `innerHTML` from diff text. **Split** view = `cloneNode(true)` of already-escaped `.tx` nodes bucketed by `data-pair`/`data-side` into a 4-col grid `[old# | old code | new# | new code]`. **Search** = walk `.tx` text nodes, wrap matches with `<mark>` via DOM `Range`/`splitText`+`createElement` (clear via `normalize()`). **Copy** = read `.textContent` (decodes escaped HTML back to literal source, inert) joined with signs. **File rail** = `getAttribute('data-*')` + `textContent` writes only.
3. Diff text stays out of `window.SKILL_AB` entirely; only counts/ids/flags ride the blob.

### 1.5 Interactions (one `DiffViewer` IIFE appended to `_HTML_SCRIPT` at `:2684`, invoked beside `wireTooltip()`)

- **Modes:** `Compare` (two sync-scrolled panes, default), `Focus` (one arm full-width — room for Split), `Diff-of-diffs` (synthesized tri-color pane from `diffofdiffs` data). Mode = `data-mode` class flip.
- **Sync-scroll (Compare):** `scrollTop`-ratio mirror across same `data-task` group, rAF-throttled, reentrancy flag (ratio not pixel — panes differ in height).
- **Split/Unified, Wrap:** class toggles, persisted per-file in `localStorage` (works under `file://`).
- **File rail + scroll-spy:** JS scans `[id^=f-]`, builds a sticky rail (or `<select>` on narrow viewports), each entry tagged with its `diverge` verdict + a deterministic **beat caption** (graft, §1.6); `IntersectionObserver` highlights the current file.
- **Folds:** "Expand N unchanged lines" reveals kept context; honestly disabled where git already elided (report only has captured `git diff`, not the repo — documented, no fake fetch).
- **Copy / permalink-to-line:** per-file + per-hunk Copy (clipboard API → hidden-textarea `execCommand('copy')` fallback, **mandatory** for `file://`). Gutter click sets `location.hash=#L<file>-<o|n>-<num>`, opens enclosing `<details>`/fold, scrolls + `.hl`-flashes.
- **Minimap (graft):** `<canvas>` painted from `data-k` ints, viewport rect tracks scroll, click/drag scrubs both panes.
- **Optional overlay kernel (graft, reduced-motion-gated, wrap-disabled):** in Compare, a Flip control over hunk-aligned shared files only where row heights match — an optional flourish, never primary.
- **Keyboard (one guarded `keydown`, no-op when `e.target` is input/textarea/select):** `j/k` hunk, `n/p` file, `u` split, `w` wrap, `/` search, `c` copy focused file, `m` cycle mode, `g/G` top/bottom, `?` help legend.

### 1.6 Beat captions + contrast band (grafts from Divergence)

- **Contrast band** at the top of each task: dual ON/OFF scorecards (tests/lint/build pills, files, +A/−D, turns, $cost from each arm's representative `RunResult`) + a one-line **templated thesis** assembled from score/size deltas (fixed vocabulary, e.g. *"skill arm made tests pass with a smaller, test-backed change"*) + the clickable divergence map. Surfaces the verdict before a line is read. Fully deterministic, ints + escaped filenames only.
- **Beat captions** on rail entries via two stdlib lookup tables: path/ext → category (`test_*`/`*.test.*`→tests, `*.md`→docs, `package.json`/`pyproject.toml`/`go.mod`→manifest, `*.lock`→lockfile, `.github/*`/`Dockerfile`/`*.yml`→CI, else source) and change-shape → phrase (new/deleted/pure-add/N-line rewrite/targeted edit). Factual labels only ("added test_parser.py, +40/−0; only skill arm touched it") — never authoritative "what it means."

### 1.7 CSS / visual treatment (into `_HTML_STYLE` at `:2330`)

New tokens in `:root` **and** the dark `@media` (`:2349`): `--hunk`, `--w-add`, `--w-del`, `--diverge`, `--accent` (replaces hardcoded `.hunk{color:#8957e5}` at `:2646` and tab10 blue). Rows = CSS grid `[ln ln sg tx]`; `.ln,.sg{user-select:none}` so copy excludes gutter; `position:sticky` left gutter + sticky `.file-head`; add/del get a **left accent border** instead of full-bleed 12%-alpha wash; `.w` uses the stronger word tokens; `:focus-visible` ring on summaries/buttons/gutters; **mandatory** `@media (prefers-reduced-motion:reduce)` gate for `.hl` flash/scroll/overlay (graft — report has none today).

### 1.8 Tests (extend `test_skill_ab_harness.py`, stdlib)

`parse_unified_diff` line-number seeding on a crafted multi-file/multi-hunk diff; `word_diff_pairs` `pair_idx`/`data-pair` faithfulness + per-segment escaping (assert `<` in a renamed identifier emerges as `&lt;` inside `.w`); ratio-gate + 400-char cap fallbacks; `diffofdiffs` agree/a-only/b-only counts; truncation marker emitted when `len(stdout) >= judge_max_diff_chars`.

---

## 2. Treatment / inputs panel

Make the *independent variable itself* legible: both arms ran the same `claude -p "<prompt>"`; exactly one thing differed.

### 2.1 What to persist — per-EXPERIMENT, never per-run

Do **not** touch `RunResult.to_dict` (`:422`) — it is written K×tasks×arms and already carries `diff`; adding multi-KB guidance there bloats `results.jsonl`. Instead add a top-level **`treatments`** block to `experiment_manifest()` (`:1998`), computed once, already embedded into `summary.json` and passed to `build_html_report`.

**Signature change:** `experiment_manifest(cfg, seed=0, timestamp=..., offline=False, tasks=None)`. Thread `tasks` at every call site that has them: `_run_and_outputs` (`:3925`), the `report` subcommand (`:4271`, tasks from `load_config`), and widen `_finalize_run` (`skill_ab_server.py:383`) to accept/forward `tasks` from its caller `_run_real` (`:413`). Offline/demo paths skip subprocess/file IO but still emit `shared_prompt` + skill identity (pure from cfg+SKILL.md).

```python
treatments = {
  "shared_prompt": {task_id: html-unescaped prompt text},   # the identical -p arg
  "isolation": cfg.isolation,                                 # "inject" | "worktree"
  "arms": {
    "off":  {"role":"control", "argv":[...], "added":[]},
    "on":   {"role":"treatment", "argv":[...],
             "added":["--disable-slash-commands",
                      "--append-system-prompt-file <injected-guidance>"],   # inject
             "installed_skill":{name,source,path,sha256},                    # worktree
             "guidance":{"text":..., "truncated":bool, "sha256":...}}        # inject only
  }
}
```

### 2.2 Functions

- **Reconstruct guidance deterministically** (the temp inject file is unlinked at `:1138`): for inject arms re-derive `injected_system_prompt(resolve_skill(...))` (`:706`) over `prepare_skill_guidance` (`:694`) — pure function of the SKILL.md the manifest already hashes (`:2015`), so the deleted temp file is irrelevant. Cap stored text at a `judge_max_diff_chars`-style limit with a `truncated` flag; lean on the existing sha256 for integrity. For worktree arms record `installed_skill` from `arm_skill(cfg,arm)`/`list_available_skills` + a note that Claude loads guidance at runtime.
- **DRY argv extraction (prevents drift):** extract `build_agent_argv(task, cfg, *, inject_file, disable_skills, model) -> list[str]` from `run_agent` (`:855`–`877`). `run_agent` calls it for real; the treatments builder calls it with sentinel `inject_file='<injected-guidance>'` so the displayed argv matches reality without leaking the ephemeral temp path. Runner arms record `_runner_label(runner)` + "prompt fed via stdin/{prompt_file}".

### 2.3 Render — server-side + escaped (new `_treatment_inputs_html`)

Add next to `_work_products_html` (`:3585`); `html.escape()` the prompt, every argv token, the guidance body, and skill identity. **Prepend** to `server_detail` at `:3690` so it lands in the static body **outside** `#app` — guidance text never enters `window.SKILL_AB` nor any `innerHTML`-of-raw path. No `skill_ab_app.py` change (Results iframes report.html).

Guided "only thing that differed" layout, reusing `.section-h`/`.det`/`.cols`/`.col`/`.mono`:
1. One **shared** block: `claude -p "<prompt>"` (escaped; `<details>` if long).
2. A 2–3 col per-arm row: control = "baseline — nothing added"; skill arm = the added tokens wrapped in a new `.added` span (`var(--good)`), or "installed `.claude/skills/<name>/`" for worktree.
3. Full injected guidance inside a collapsed `<details>` below. New `.added` CSS rule uses theme vars (dark/light safe, no inline SVG).

---

## 3. Guided user flows (ranked by impact)

1. **[app] Errored/aborted runs must not iframe raw 404 JSON.** `viewResults` (`skill_ab_app.py:1138`) frames `/api/runs/<id>/report` unconditionally, but report.html only exists on success (`_finalize_run`, `skill_ab_server.py:392`); error/abort paths terminate without it, so `_send_file` returns `{"error":"not found"},404` (`:657`) into the frame. **Fix:** before framing, check status from `/api/runs` (or HEAD the report); on error/aborted/404 render an error card (verdict pill + failure message + "Start a new run" / "Back to Dashboard") and add an `onerror`/load-timeout iframe fallback. *(high)*
2. **[app] Surface `claude` pre-flight on the New-run path.** `viewNew` (`:614`) never reads health; a novice configures, Estimates (always succeeds — it's a projection), Starts, and only discovers `claude` missing as a failed Live run. **Fix:** call/cached-read `refreshHealth` (`:530`); when `claude_on_path` false (`server:764`) show a warning banner + disable the real Start (leave the demo button active). *(high)*
3. **[app] Progressive disclosure on the New-run form.** ~10 controls dumped flat (`:836`–`896`). **Fix:** wrap expert controls (Agent CLI · Arm B, Isolation, cost ceiling, blind judge, per-arm Model B) in `<details class="advanced"><summary>Advanced options</summary>`, collapsed by default; leave Skill A, Target, Prompt, k visible. Pure HTML/CSS. *(high)*
4. **[app] Inline required-field validation.** Skill A is mandatory with no marker; the only feedback is a 4.2s corner toast (`:778`–`784`, auto-removed `:442`). **Fix:** required asterisk on Skill A (+ conditional Prompt); in `badInputs()` set `aria-invalid` + a persistent `.field-error` span under the field, cleared on next input. *(high)*
5. **[app] Promote the verdict on Results.** The badge `<img>` + "Copy badge markdown" dominate; the actual verdict lives below the fold inside the iframe. **Fix:** fetch the run card/summary in `viewResults`, render a headline row above the frame — `skillTitle(a,b)` + `verdictPill(verdict)` + primary-metric delta (reuse helpers `:501`); demote badge-markdown to a secondary block. *(med)*
6. **[app] Results iframe loading state.** No spinner while the large self-contained report parses — blank box. **Fix:** position container + overlay `loadingEl('loading report…')` (`:565`), remove on iframe `load`. *(med)*
7. **[app] Live-view progress + elapsed.** No "X of N cells", no timer; the ticker bar only fills when a ceiling exists (`setTicker` pct=0 otherwise, `:519`). **Fix:** total cells from `experiment_start`, completed count in `run_done`/`run_skipped`; "n / total cells" header + CSS bar + `setInterval` elapsed timer + carried-over projected wall time from the estimate. *(med)*
8. **[app] Auto-refresh running Dashboard cards.** `viewDashboard` (`:547`) fetches once. **Fix:** if any card is `running`, `setInterval(4s)` re-fetch/re-render, cleared via the existing `cleanup` route hook; add a manual Refresh button. *(med)*
9. **[app] "How it works" onboarding card.** Empty state jumps to jargon (arm/cell/control/isolation/judge) unexplained. **Fix:** static 3-step card (pick a skill → run K× with/without on the same task → read the verdict) above the empty state, demo CTA framed "See it end-to-end with zero spend." *(med)*
10. **[app] Guard the Results badge `<img>` for inconclusive/errored runs.** badge.svg only written when verdict is truthy (`server:394`); `viewResults` builds it unconditionally → broken-image glyph. **Fix:** `onerror` hides it / swaps a neutral "inconclusive — no badge" pill; hide Copy-badge when absent, mirroring `runCard`'s existing guard (`:588`). *(low)*

---

## 4. Visual design upgrades

Tokenize, then map. All in `_HTML_STYLE` `:root` (`:2331`) + dark block (`:2349`); `_APP_CSS` inherits.

- **[engine/report] Accent ramp.** Define `--accent-50..600`, `--accent`, `--accent-ink`, `--accent-ring`; replace all ~32 raw `#1f77b4` (25 in app, 7 in report; hero `.accent` `:2436`, `arm_pal` `:3518`) with `var(--accent…)`. Shift hero to a richer indigo/azure so charts/chrome stop reading as default matplotlib. Keep `arm_pal` as *data* but source arm 0 from the token. *(high)*
- **[report] Dark-mode accent override.** Brand blue is reused unchanged on `#0b0d11` (muddy). Once tokenized, raise accent lightness ~12–18% + ring alpha in the dark block. *(med)*
- **[engine/report] Type scale.** Collapse the 16-value px soup (10…40, half-pixels) to `--text-xs:11.5 / -sm:12.5 / -base:14 / -md:16 / -lg:21 / -xl:clamp()`; add `--leading` + `--tracking` for consistent heading tracking. *(high)*
- **[report] Weight collapse.** 11 non-standard weights (640/650/660/680…) snap to the same installed face on system fonts. Reduce to `--fw-normal:500 / --fw-medium:600 / --fw-bold:700`; find-and-replace. Keep `-webkit-font-smoothing:antialiased` (`:2374`). *(med)*
- **[report+app] `prefers-reduced-motion` gate.** Zero today. Add one guarded block per file neutralizing the live cell-scan loop (`app:244`), spinner (`:209`), toast rise (`:309`), arm hover lift (`report:2490`) — and gate every new diff-viewer animation. *(med)*
- **[report+app] Focus-visible rings.** Report has none; app has 2. Add `--accent-ring` + `:where(a,button,summary,[tabindex],select,input):focus-visible{outline:none;box-shadow:0 0 0 3px var(--accent-ring)}` per file. *(med)*
- **[report+app] Motion tokens.** `--ease:cubic-bezier(.2,.8,.2,1)`, `--dur:.16s`; apply to hover/lift; add `:active` press (`.run-card:active,.arm:active{transform:translateY(0) scale(.997)}`); opt-in fade-up on grids, gated by the reduced-motion block. *(med)*
- **[report+app] Radius scale.** Replace ~15 literals with `--radius-xs:7 / -sm:10 / :14 / -lg:18 / -pill:999`. *(low)*

---

## 5. Prioritized roadmap

Diff viewer + treatment panel first (the explicit asks).

| # | Item | Tag | Scope | Effort |
|---|------|-----|-------|--------|
| 1 | `parse_unified_diff` + `_Row/_Hunk/_PatchFile` model; replace `_diff_to_html`; line-number gutter + sign column + file/hunk headers with id anchors | [engine][report] | Parser foundation everything else builds on | **L** |
| 2 | `word_diff_pairs` (ratio-gate, 400-char cap) + `<span class=w>` per-segment-escaped highlight | [engine][report] | Intra-line highlight | **M** |
| 3 | Context folding + honest truncation marker (`diff_truncated` on RunResult) | [engine][report] | Collapse noise, stop silent edit loss | **S** |
| 4 | `_work_products_html` → `.cmp` shell: arm/run `<select>`, sync-scrolled Compare/Focus modes, sticky file rail + scroll-spy | [report] | Arm-vs-arm core | **M** |
| 5 | `DiffViewer` IIFE: split/unified (clone), wrap, search (Range/splitText), copy (+execCommand fallback), permalink, keyboard, localStorage | [report] | Interaction layer (escape-safe) | **L** |
| 6 | Contrast band + divergence map + beat captions (deterministic lookup tables) | [engine][report] | Verdict-before-you-read | **M** |
| 7 | `diffofdiffs` server compute + tri-color synthesized mode | [engine][report] | Compare-first centerpiece | **M** |
| 8 | Canvas minimap from `data-k` ints + viewport scrub | [report] | Large-diff navigation | **S** |
| 9 | Diff-viewer unit tests (pairing/escaping/truncation/diffofdiffs) | [engine] | Lock the safety claims | **S** |
| 10 | `treatments` block in `experiment_manifest` + thread `tasks` through all call sites incl. `_finalize_run` | [engine] | Persist the IV | **M** |
| 11 | `build_agent_argv` extraction (DRY) + deterministic guidance/skill reconstruction | [engine] | Drift-proof argv/guidance | **S** |
| 12 | `_treatment_inputs_html` guided "only thing that differed" panel + `.added` CSS | [report] | Render the IV | **S** |
| 13 | Accent + type + weight + radius tokenization; replace literals | [engine][report][app] | Modernize visual language | **M** |
| 14 | `prefers-reduced-motion` gate + focus-visible rings + motion tokens (both files) | [report][app] | a11y/polish floor | **S** |
| 15 | Errored/aborted Results error card + iframe `onerror`; badge `<img>` guard | [app] | Stop broken Results screens | **S** |
| 16 | New-run `claude` pre-flight banner + disable real Start | [app] | Prevent doomed runs | **S** |
| 17 | New-run progressive disclosure (`<details class=advanced>`) + inline required validation | [app] | Form usability | **S** |
| 18 | Results verdict headline + iframe loading overlay | [app] | Answer "did A beat B?" up front | **S** |
| 19 | Live progress/elapsed header; Dashboard auto-refresh; "How it works" onboarding card | [app] | Live + first-run guidance | **M** |

Relevant files (all absolute): `/Users/copyjosh/Code/skills-test/skill_ab_harness.py`, `/Users/copyjosh/Code/skills-test/skill_ab_app.py`, `/Users/copyjosh/Code/skills-test/skill_ab_server.py`, `/Users/copyjosh/Code/skills-test/test_skill_ab_harness.py`, `/Users/copyjosh/Code/skills-test/test_skill_ab_server.py`.