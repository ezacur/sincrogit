# ⏳g SincroGit — Design document

> An automatic, versioned time machine for your repos (and low-effort multi-machine
> continuity), with **zero** Git discipline required.
> Target platform: **Windows** (interactive use, one machine at a time).

> **How to read this document.** §1–§11 describe the system **as built** and are kept in
> sync with the code (§1 keeps the original first-person goals; §9 is the deployment
> *plan* — the one Phase-3 piece still pending). §12 (roadmap) and §13 (decision log)
> record **history**: their entries are not rewritten retroactively — when reality moved
> on, an explicit *(since superseded …)* note says so. For day-to-day operation see the
> [Manual](MANUAL.md); for the configuration reference, the
> [README](README.md#configuration); for how SincroGit relates to neighboring tools
> (jj, GitButler, dura, …) and for the pending-work list, the README's
> [How it compares](README.md#how-it-compares-with-neighboring-tools) and
> [TODO](README.md#todo) sections.

---

## 1. Goals and scope

**I want two things at once:**

1. **Versioning + a time machine, with zero discipline.** Roll back any *saved* file to an earlier state (you broke/deleted/overwrote something) or to yesterday — without ever running `git`. (It snapshots what's on disk, not unsaved editor buffers; a power cut with an intact disk loses nothing regardless — the value is the rollback, not crash survival. A *total disk failure* is the one case the remote mirror covers, and it's rare.)
2. **Sync between machines (sequential).** I work almost always on the desktop and occasionally on the laptop, **never both at once**. When switching machines, I want the sources to update automatically (within minutes — see §4.2; not instant).

**Out of scope (for now):**

- Simultaneous editing on two machines / complex conflict resolution.
- Versioning binaries or large files (those I commit/push **by hand**).
- Any OS other than Windows. On Linux/others I'll `pull` by hand and won't edit.

---

## 2. Conceptual model: two tiers (the "shadow" design)

The trick to reconcile *"almost instant snapshot"* with *"I don't want thousands of commits"* — AND with *"my `git log`/`git status` must stay mine"* — is to separate two tiers, keeping the fast one OFF the user's branch entirely:

| Tier | What it is | Frequency | Visible in `git log` |
|------|-----------|-----------|----------------------|
| **Snapshot (shadow)** | A commit built through a **private index** (`.git/sincro-index`) and appended to a **side ref** `refs/sincro/wip/<branch>` — HEAD, the user's index and worktree are never touched | Every ~5 min (with debounce) | No (side ref; every git tool sees a normal repo) |
| **Sealed (history)** | The accumulated snapshot tree is committed as **one real commit** on the branch (`commit-tree` + `update-ref`); the shadow chain **re-anchors** there | Every ~6 h | Yes (permanent commit) |

```
branch:  ... ── sealed_N ─────────────────────── sealed_N+1   ← HEAD (only real commits)
                     │                                ▲
shadow:              └── s1 ── s2 ── s3 ── … ── s42 ──┘  refs/sincro/wip/<branch>
                        (a snapshot commit every ~5 min; at the seal, the chain
                         re-anchors at sealed_N+1 and the old one stays in the
                         side ref's reflog for ~30 days)
```

**Why it works:**

- The current saved state is committed every ~5 min → a **rollback point** at ~5 min resolution (the shadow tip = last snapshot; earlier ones are real commits on the chain, pre-seal chains in the side ref's reflog). NB: this is *not* power-cut protection — saved files survive a power cut on the disk anyway, and unsaved buffers are never captured; the value is the time machine.
- The snapshots never appear on the branch: only **~4 commits/day** land there (one seal every 6 h) — and `git status` keeps showing the user's real uncommitted changes, their staging area untouched.
- The "clean" history (sealed) is the only thing that travels to the remote's branch → **pull always clean, no force-push** (see §4).

> **History of this design:** v0.1 kept the snapshot as a single WIP commit *at the tip*, amended in place. It worked, but it occupied HEAD (confusing every git tool and hijacking `git status`/staging). v0.2 moved the snapshots to the shadow ref — same rhythms, same recovery windows, invisible. Old repos are migrated automatically at startup (the WIP tip moves to the shadow ref and the branch returns to its parent; unsealed edits reappear as ordinary uncommitted changes).

---

## 3. Detailed flow

### 3.1 Service startup (per repo)
1. Validate it's a git repo (a missing or invalid folder skips that repo; the others keep
   running). The remote is checked lazily on each sync (`has_remote`); being on the
   configured branch is the branch guard's job (§11).
2. **Migrate a legacy WIP tip** if present (v0.1 repos; see §2) and ensure the **shadow
   ref** exists (anchored at HEAD; `core.logAllRefUpdates=always` is set locally so the
   side ref gets a reflog), then register the repo with the watcher.
3. **Initial snapshot**, before any networking: captures changes that predate this run
   (e.g. edits made while SincroGit wasn't running, or after a reboot).
4. **Initial sync on a background thread** — a slow network never delays the local safety
   net: `fetch` + (only if the remote is ahead) rebase of the local branch
   (`--autostash`), exactly like the periodic pull (§3.4).
   - **If there is a conflict** → `git rebase --abort`, the **autosync for that repo is paused**, the user is notified and it is logged. It is **never** resolved destructively, nor is force used. (This is rare in sequential use, but the policy is: when in doubt, don't lose data.)

### 3.2 Snapshot cycle (every ~5 min, with debounce)
- The **watcher** (filesystem events) marks the repo as *dirty* and resets a debounce (e.g. 20-30 s without changes).
- When the debounce settles **and** ≥5 min has passed since the last snapshot:
  1. Sync the **private index** to the shadow tip (cheap in steady state), diff it
     against the worktree — the precise "what changed since the last snapshot" — and
     apply the **filter** (§5) to the candidates.
  2. `git add <only the candidates>` INTO the private index (`GIT_INDEX_FILE`).
  3. `write-tree`; if the tree differs from the shadow tip's (textconv-aware, so a
     styling-only `.docx` resave doesn't count): `commit-tree` + `update-ref` the shadow
     ref (`sincro: snapshot`). HEAD, the user's index and `git status` are untouched.
- No changes → nothing happens.
- **Anti-starvation:** a source that never settles (a long build, a log writer inside the
  repo) keeps resetting the debounce — so past **2× the snapshot interval** since the last
  snapshot, one is taken anyway, debounce or not (`Engine.SNAPSHOT_STARVATION_FACTOR`).
  `debounce_sec: inf` keeps its "never fire" meaning.

### 3.3 Sealing (every 6 h)
**Only automatic trigger:** a timer of **6 h since the last seal**.

0. **The user's own commits count as seals.** When the timer fires, if a permanent
   (non-WIP) commit is newer than the clock's baseline — a manual `git commit` made in
   a terminal, or commits a pull integrated — the window **restarts from it** instead
   of stacking a `sincro:` checkpoint on its heels. (v0.1's "external commit detected;
   seal clock reset", ported to the shadow model; checked only when a seal is due, so
   it costs nothing in steady state. The purist commit nudge refreshes off the same
   source.)
1. If the user has something **staged** (a manual commit in progress) → **yield** this
   cycle; an auto-seal never absorbs a hand-crafted commit. (An explicit Smart Commit
   proceeds — the user asked for it.)
2. Final snapshot; if the snapshot tree matches HEAD's tree (textconv-aware) → **don't
   seal** (don't pollute the history).
3. Generate a message with AI from `git diff HEAD <snapshot-tree>` (§6).
4. `commit-tree <snapshot-tree> -p HEAD -m "<AI message>"` + advance the branch, then
   refresh the user's index (mixed reset; the worktree — which IS that tree — is
   untouched) and **re-anchor the shadow chain** at the new seal.
5. **Push** (§4).

> There is no sealing on idle or on shutdown. To force a one-off seal+push (e.g. right before I head to the laptop): *Seal now* / per-repo *Seal+Push* in the tray, `--seal-once` from the CLI, or a Smart Commit.

### 3.4 Periodic pull (every 10 min)
Besides the startup pull (§3.1), the daemon checks the remote every **10 min** to bring in what the other machine left, **without** having to log back in or pull by hand.

1. **`git fetch`** (cheap; doesn't touch the working tree).
2. Check whether the remote has new commits:
   `git rev-list --count HEAD..<remote>/<branch>`.
   - If it's **0** → nothing to bring → **do nothing** (the usual case while I work on this machine).
   - If it's **> 0** → take a **snapshot first** (the recovery guarantee for everything
     below), then **`git rebase --autostash <remote>/<branch>`** — the user's edits live
     uncommitted in the worktree, so the autostash carries them over the rebase.
3. Two conflict shapes, both → **pause autosync for that repo + notify**, resolve by hand
   (never force, never data loss):
   - the **rebase itself conflicts** → `git rebase --abort`, tree intact;
   - the rebase succeeds but **re-applying the dirty edits conflicts** → git leaves
     conflict markers in the affected files (and a stash entry); the exact pre-pull
     state is one Time-Machine restore away thanks to the snapshot in step 2.

> Since usage is **sequential** (never both machines at once), while I work on one, the other isn't pushing → step 2 returns 0 and the pull doesn't fire. When I sit at the other machine, within ≤10 min it picks up on its own what I sealed on the first.

---

## 4. Push and multi-machine

**Golden rule: only sealed commits are pushed; the WIP never leaves the machine.**

- Push: in the shadow model **HEAD only ever holds sealed and user commits** (the WIP lives
  on the side ref `refs/sincro/wip/<branch>`, §2), so pushing HEAD is safe by construction
  and never leaks the WIP: `git push origin HEAD:refs/heads/<branch>` → the remote receives
  immutable history. (An unpushed backlog rides along implicitly and retries on the next sync.)
- Because sealed commits are immutable and never rewritten, **the push is always fast-forward** and the **other machine's pull is always clean**. No force-push is needed in any normal-flow case.

**Handoff between machines (sequential use):**

```
Desktop: works → every 6h (or a manual Seal now / Smart Commit) seal + push  ──►  remote up to date
Laptop:  starts → pull --rebase (clean) → works → seal + push ──► remote up to date
Desktop: starts → pull --rebase (clean) → continues...
```

**Normal handoff (clean branch):** the laptop does `pull --rebase` and starts from the **sealed** state (up to 6 h old). For a mid-window handoff, run a manual seal (**Seal now** / `--seal-once` / Smart Commit) before getting up so the laptop starts with everything via the clean path.

### 4.1 Autosnap (live mirror) — disaster recovery

Since sealing every 6 h would leave up to 6 h of work off the remote, **autosnap** decouples the *remote backup* from the *history*: every **30 min** (and only if something changed) it `push --force`es the **shadow tip** (`refs/sincro/wip/<branch>` — sealed history **+ the live WIP**) to a **per-user, per-machine** side ref `refs/autosnap/<user>/<host>/<branch>` (the namespace is detailed in §4.2).

- **Keeps the branch clean:** nobody pulls that ref for work; `main` still receives only sealed commits → the pull is always clean. It's the deliberate exception to "the WIP never leaves the machine", scoped to a backup ref.
- **Disk-failure RPO ≈ 30 min** (instead of 6 h). On the other machine: *Fetch autosnaps* → browse/restore the latest state (single file or whole repo).
- **Cost:** up to ~48 pushes/day/repo during active work (cheap force-push; **nothing** on idle repos, since it only pushes when the shadow tip changed since the last mirror). Orphan objects on the remote until its GC.
- **Power cut / OS crash** needs nothing special from autosnap: saved files survive on the local disk, and the 5-min snapshot/`reflog` give the rollback points. autosnap is for the *machine-is-gone* case (and the handoff, §4.2).

> *("Live mirror" variant discarded for now: it did force-with-lease of the WIP every minute to keep the remote <1 min behind. More traffic and complexity; re-evaluable if real-time remote backup ever becomes critical.)*

### 4.2 Cross-machine handoff (live WIP)

The autosnap mirror is also the substrate for **automatic machine-to-machine handoff**,
decoupled from sealing (so it works in purist mode too, where the seal never fires). Two
design points:

- **Ref namespace `refs/autosnap/<user>/<host>/<branch>`.** The `<host>` keeps each machine
  the *sole writer* of its own ref (a plain `--force` is safe, no clobber). The `<user>`
  (sanitized `git config user.email`) lets a machine recognize its *own* other machines vs.
  a teammate's, so handoff fetches only `refs/autosnap/<user>/*` — cheap, and team-safe (it
  never touches `main`/feature branches, only personal side refs).
- **Compare by WORK CONTENT, not ancestry.** Critical subtlety: the two machines'
  snapshot chains are *siblings* (both rooted at the shared seal), never descendants of
  each other — ancestry checks would report divergence constantly. Instead
  `GitRepo.work_relationship(mine, theirs)` compares the two shadow tips,
  relative to the merge base, by the *paths each side changed*: if `theirs` matches `mine` on
  every path I changed (and has more) it's `theirs_contains` → safe to adopt; the mirror is
  classified `equal` / `mine_contains` / `diverged` otherwise.

Behavior — `live_handoff` is a 3-state knob (`auto` default | `ask` | `off`):
- **`theirs_contains` → safe apply, content-first**: the WORKTREE is made to match the
  peer's snapshot tree (worktree-only writes + deletes of the differing paths — the
  user's HEAD and branch never move; sealed history reconciles via the normal pull) and
  a closing snapshot records it in MY chain. Provably loss-free (the peer holds all my
  changed-path content), reversible via the shadow reflog, and **refused (with a
  notification)** where it would touch content the snapshots don't hold
  (`untracked_collisions`, or filter-refused local edits — those exist nowhere in git,
  so overwriting would destroy them). In `auto` it's applied immediately **and a tray notification is
  fired** (level b is never *silent* — the working tree changing under you is a surprise even
  when nothing is lost). In `ask` it is NOT applied: the candidate is recorded
  (`pending_handoff`, surfaced in `status()` and the panel), the user is notified, and a
  one-click **Apply** (`Engine.apply_handoff` / `--apply-handoff`) re-validates from scratch
  (re-fetch + re-classify + re-check collisions, since the peer may have moved) before the
  apply (level a / consent).
- **`diverged` → notify, never auto-merge.** Deliberately no 3-way auto-merge of two piles of
  unreviewed in-progress work (a quiet, subtly-broken tree is the worst outcome). SincroGit
  warns **once** per distinct peer state and leaves both intact; the user resolves by
  **Smart-Committing one side then syncing** (normal rebase, with the usual conflict-pause),
  or by inspecting/merging the side ref by hand. See the README.

Runs at the end of the sync cycle (so it needs `pull` or `push` on to fire) and inside the
repo's `op_lock`. Phase 2 levels (a)+(b); a true auto-merge mode is intentionally out of
scope.

**Made prompt by OS events (cuts the interval latency from ~40 min to seconds).** Two halves,
keyed off the moments that bracket a machine switch:
- **Leaving** (Windows session **lock** or **suspend**): `Engine.flush_now()` forces a
  snapshot + autosnap push *now* (ignoring the interval) on a background thread, so the remote
  mirror is fresh in seconds. Best-effort on suspend (~2 s before the network dies; the normal
  autosnap interval is the backstop); reliable on lock.
- **Left for real** (locked and away ≥ `seal_on_leave_min`, default 20 min): the
  **leave seal**. The lock arms a wall-clock countdown (wall, not monotonic: it must
  keep counting across a suspend); unlock/resume disarms it; firing runs the normal
  seal rules off-thread with a `sincro: [leave]` title and pushes. Arming never
  touches the 6 h clock — if the regular seal (or a manual commit) lands first, the
  leave seal finds nothing to publish and moves NO clock. At most once per repo per
  absence; flat OFF in purist mode (the branch stays 100 % the user's). An imminent
  SUSPEND with the countdown running fires it immediately with the deterministic
  message (no AI: the ~2 s grace would lose the commit), because a sleeping machine
  has no timer.
- **Leaving for good** (**shutdown / restart / logoff**): `WM_QUERYENDSESSION` /
  `WM_ENDSESSION` (both, deduped — a critical shutdown may skip the first) trigger a
  SYNCHRONOUS `flush_now(wait=True, wait_timeout=20)`: the process dies when the handler
  returns, so async would silently lose the push. `ShutdownBlockReasonCreate` shows
  "backing up your latest work" on the shutdown screen while it runs (without it Windows
  kills a GUI process ~5 s in); the 20 s bound guarantees the shutdown is never hostage.
  An `ENDSESSION(FALSE)` (some app vetoed) re-arms the hook.
- **Arriving** (**unlock** / **resume**): `Engine.sync_soon()` makes a fetch/pull/handoff due
  on the next tick and wakes the loop, so the peer's work is picked up at once.

The triggers: a Windows `QAbstractNativeEventFilter` (in the tray app) catches
`WM_WTSSESSION_CHANGE` (lock/unlock, via `WTSRegisterSessionNotification` on the panel's HWND)
and `WM_POWERBROADCAST` (suspend/resume); leave→`flush_now`, arrive→`sync_soon`, debounced
(lock often precedes suspend; resume precedes unlock). A **wall-clock-gap detector** in the
engine loop (dependency-free) also fires the arrive path after any long suspend — so the wake
side works headless too (monotonic clocks may freeze across suspend; the wall clock doesn't).

---

## 5. File filter: code only

**Criterion: only TEXT and < 1 MB is versioned automatically.** Everything else (binaries, large files) I manage **by hand**.

- **"Text" detection** by content, not by extension (more reliable than an extension list). Since the size filter already guarantees the file is ≤ 1 MB, a large prefix is inspected (up to ~1 MB, not just a few KB) and classified in layers: empty → text; a Unicode **BOM** (UTF-8/16/32) → text; a **NUL** byte → binary; otherwise decide by the **proportion of control bytes** (those < 0x20 that aren't whitespace/tab/newlines, + DEL): very few → readable text (incl. UTF-8 with accents/emoji/CJK and Latin-1); many → binary. *(Limitation: UTF-16/32 **without** a BOM contains NUL bytes → treated as binary, same as git.)*
- **Size:** discard if > 1 MB.
- **Key implementation:** the filter lives in the **tool's `git add` logic** (it runs `git add` *only* on files that pass the filter; **never** `git add -A`).
  - Advantage: since I **don't** use `.gitignore` for this, if one day I want to add a binary or a large file, I just `git add <file>` by hand and commit — the tool doesn't stop me, it simply doesn't touch it on its own.
- Configurable: maximum size and extra exclusion patterns (e.g. `node_modules/`, `.venv/`, `dist/`).
- **Smart Ignore (`suggest_excludes`, default on):** the filter reports each rejected file (binary/too-large, not a user exclude) to the engine, which buckets them by top-level folder. When one folder accumulates **≥ `NOISE_SUGGEST_THRESHOLD` (50)** distinct filtered-out files — almost always build output or a cache — it **suggests once** (a notification + log) adding `**/<folder>/**` to `extra_excludes`. It never auto-edits the config, fires at most once per folder per session, and counts only *rejected* files (a big text refactor passes the filter, so it never trips). Catches noise the default excludes miss without nagging.
- **Include list (`extra_includes`)**: patterns versioned **even if binary** (e.g. `**/*.docx`), under a separate size cap (`max_include_bytes`, 25 MB). For `.docx` and similar, SincroGit maps the file to a **pandoc `textconv` diff driver** in `.gitattributes` (committed, travels) and injects the textconv command **inline** (`git -c diff.pandoc.textconv=…`) on every diff → readable (markdown) diffs with no per-machine `git config`; this feeds the AI seal messages and the time-machine. The `.docx` stays the source of truth; the markdown is a *lossy* view. Pandoc's path is configurable (`pandoc_path`, per machine); without pandoc it degrades to versioning the opaque blob. **Consequence:** since change-detection uses that diff, a `.docx` is versioned/synced **only when its markdown changes** (text and structural formatting: bold, headings, lists, tables); purely visual styling (font/color/layout) and Word's resave churn don't trigger a version until a content change carries them in.
- **`.pptx` (convert.py)**: same opt-in, different converter — an **in-process extractor** over `python-pptx` (optional dependency; MIT, bundles into the exe) renders slides as markdown (titles, bullets with indent level, tables, speaker notes) for the GUI previews/diffs/search and "Save a copy". Deliberately NOT a git textconv driver: that requires an external executable git can spawn (pandoc's role), and a Python entry point pays interpreter startup per invocation — unacceptable inside `git diff`. Consequences: the AI seal diff sees `.pptx` as binary (`--stat`), and change detection is by **bytes** (every resave versions), unlike the md-gated `.docx`. The pptx→docx→pandoc chain was evaluated and rejected: pandoc can't read pptx, so it needs LibreOffice/Office COM as a third conversion stage — heavier and less deterministic than reading the XML directly.

---

## 6. AI commit messages (hybrid mode)

They are generated when sealing (automatic) and on a **manual commit** (Smart Commit).

**Hybrid strategy (chosen):**
1. If **Ollama** is available locally → use it (free, no quota, **the code doesn't leave the machine**).
2. Otherwise → fall back to a **cloud** provider (Gemini) with an API key.
3. **Privacy mode:** option to send to the cloud **only `git diff --stat` + file names** (not the content), so as not to expose sensitive code.
4. **Fallback always available:** if the AI fails (no network, no quota, timeout) → automatic deterministic message, e.g.:
   `sincro: 4 file(s) (1 modified, 1 new, 1 deleted)`.
   **The commit/seal is never blocked because of the AI.**

**Two prefix conventions (key to tell machine from human apart):**
- **Automatic seal → `sincro:`** (a time-bucket; we don't pretend to classify it as `feat`/`fix`). The prefix marks it as a machine commit.
- **Manual commit (Smart Commit) → Conventional Commits** (`feat:`/`fix:`/`docs:`/`refactor:`/…). The user triggers it from the GUI, the AI **proposes** an editable message, and on confirm the current WIP is sealed with it and the **6 h seal timer is reset**.

**Model input:**
- *Automatic seal:* `git diff <HEAD-tree> <snapshot-tree> --stat` + truncated diff (the window being sealed: HEAD's tree → the latest snapshot tree) → a concise `sincro:` message.
- *Manual commit:* diff **since the last manual commit** (skipping the `sincro:` seals) up to the WIP → the AI summarizes the whole unit of work. The commit only contains the WIP delta, so the body honestly notes it's a **cumulative summary** (some of the code is in earlier `sincro:` seals).

---

## 7. Software architecture (Python)

```
sincrogit/
├─ sincrogit/
│  ├─ __main__.py        # entrypoint / CLI (tray, headless, --history, --autosnaps, ...)
│  ├─ config.py          # loads/validates YAML config
│  ├─ runtime.py         # exe config, single instance (+ handshake), console
│  ├─ gitrepo.py         # git wrapper (subprocess): snapshot/seal/autosnap/restore
│  ├─ watcher.py         # watchdog + per-repo debounce (only marks "dirty")
│  ├─ engine.py          # orchestration: tick snapshot 5min / seal 6h / autosnap 30min / sync
│  ├─ filefilter.py      # text + size detection
│  ├─ messages.py        # deterministic fallback commit message
│  ├─ ai.py              # AI providers (ollama/gemini) + fallback
│  ├─ events.py          # structured event log (JSONL) for the GUI
│  ├─ log.py             # logging to a rotating file
│  ├─ notify.py          # Windows notifications (toasts)
│  ├─ convert.py         # in-process readable-text extraction (.pptx via python-pptx)
│  ├─ doctor.py          # --doctor health check (git/remotes/credentials/AI/daemon)
│  ├─ views.py           # read-only CLI views: `status` and `log`
│  ├─ autostart.py       # start-at-login (per-user Run key; §9)
│  └─ gui/               # tray icon + control panel (Time machine / Settings tabs
│                        #   inline) + dialogs (add-repo, machines, smart-commit, hunks)
├─ tests/               # pytest suite (throwaway git repos + offscreen Qt); `pytest`
├─ config.example.yaml
├─ pyproject.toml
└─ DESIGN.md
```

**Libraries:**
- **`watchdog`** — filesystem events.
- **git via `subprocess`** (not GitPython) — exact control of the plumbing snapshots/`update-ref`/`rebase`, transparent and predictable behavior.
- **standard `urllib`** — cloud AI calls; local **Ollama** client over HTTP (no extra dependency).
- **`pyyaml`** — config.
- **`PyQt5`** — tray icon + control panel (only for `--tray`).
- **`logging`** (to a rotating file) + **`winotify`** toasts (with Qt tray balloons as the in-app fallback) — alerts (e.g. "autosync paused due to conflict").
- Scheduling: own loop with timers.

**Decision:** wrap the `git` CLI with `subprocess` instead of GitPython, because the fine-grained operations (plumbing snapshots, surgical ref updates, rebase with a conflict policy) are clearer and more robust with the CLI.

---

## 8. Configuration (example)

```yaml
# config.yaml
defaults:
  snapshot_interval_sec: 300     # how often a snapshot lands on the side ref (5 min)
  debounce_sec: 25               # wait after the last change before snapshotting
  seal_interval_min: 360         # "real" commit + push every 6h (permanent timeline)
  pull_interval_min: 10          # fetch every 10 min; pull only if there's something new
  autosnap: true                 # live mirror of the latest snapshot to refs/autosnap/<user>/<host>/<branch>
  autosnap_interval_min: 30      # force-push the mirror every 30 min (only if it changed)
  max_file_bytes: 1048576        # 1 MB
  extra_excludes:                # in addition to the text/size filter
    - "**/node_modules/**"
    - "**/.venv/**"
    - "**/dist/**"

ai:
  mode: hybrid                   # hybrid | local | cloud | none
  cloud_provider: gemini         # chosen: Gemini
  cloud_model: gemini-2.5-flash-lite  # fast and within the free tier
  cloud_send_content: false      # false => only --stat + names (privacy)
  # api_key via env var SINCROGIT_GEMINI_KEY, NOT in the file

repos:
  - path: "C:/repos/sincrogit"
    remote: origin
    branch: main
  - path: "C:/repos/foo"
    remote: origin
    branch: main
    seal_interval_min: 60        # per-repo override
```

> The **API key never goes in the YAML** → environment variable (`SINCROGIT_GEMINI_KEY`, etc.).
>
> *(Abridged example — the complete, commented key set lives in
> [config.example.yaml](config.example.yaml); the README has the reference table.)*

---

## 9. Running in the background on Windows

**Yes, it can run in the background.** Options, from simplest to most "service-like":

| Option | How | Pros | Cons |
|--------|-----|------|------|
| **Per-user `Run` registry key** ⭐ | `HKCU\...\CurrentVersion\Run` → `"SincroGit.exe" --tray -c "config.yaml"` | Runs **in your user session** → sees your **SSH keys / Credential Manager**. No elevation, stdlib `winreg`, idempotent, and Windows lists it in **Task Manager → Startup apps** (user-toggleable). | Only runs while logged in (enough: you only edit while logged in). |
| **Scheduled task "at log on"** | Task Scheduler → trigger *At log on*, hidden window, automatic restart | Same session/credentials story; adds delayed start + restart policy. | Creating an at-logon task often needs elevation; less discoverable than the Startup-apps list. |
| **Shortcut in the Startup folder** | `.lnk` in `shell:startup` | Simple, visible as a file | Creating a `.lnk` programmatically needs COM; nothing the Run key doesn't do. |
| **Real Windows service** (NSSM or `pywin32`) | NSSM wraps the script as a service | Starts without login | ⚠️ Runs as *LocalSystem* → **doesn't see your SSH keys / user credentials** → the **push fails**. You'd have to configure machine-level credentials. |

**Decision (as built): the per-user `Run` key** (`autostart.py`). The scheduled task
was the original sketch, but its two extras buy nothing here — the single-instance
mutex already dedupes double launches, and the engine tolerates starting before the
network is up (pull retries on its intervals) — while the Run key needs no elevation
and stays user-visible. In a source checkout the registered command is
`pythonw.exe -m sincrogit --tray` (no console window); frozen, it's the exe itself.

> **Status: shipped.** The Settings checkbox ("Start SincroGit when I sign in to
> Windows") and `--autostart on|off` both write the key; it is deliberately NOT part
> of `config.yaml` (per-machine — the command embeds this machine's exe and config
> paths, and the YAML may travel between machines). `--doctor` reports the state,
> including a **stale** entry (target gone); the tray self-heals that case at startup
> by re-registering itself — but never touches a live entry pointing elsewhere.

---

## 10. Failure recovery

| Scenario | What happens | How I recover |
|----------|--------------|---------------|
| **Power cut / OS crash (disk intact)** | Saved files are on the disk; the last snapshot (≤5 min) is on the shadow ref `refs/sincro/wip/<branch>` | Nothing to recover for saved files (the disk has them). For *rolling back* a bad saved state: File history, or the shadow ref's reflog (≈5 min resolution). Unsaved buffers are your editor's job. |
| **"I want yesterday's version"** | It's in the sealed commits | `git checkout`/`git restore` from the matching sealed commit. |
| **I deleted something 20 min ago (within the window)** | The previous snapshot became *unreachable* in the reflog | `git reflog` + `git checkout`. *(More convenient with the optional `autosnap` branch, §12.)* |
| **Total disk failure** | Sealed state is on the remote; the latest state (≤30 min) is in the `autosnap` ref (§4.1) | On another machine: *Fetch autosnaps* → restore (file or whole repo). Max loss ≈ 30 min. Without autosnap: down to the last seal (6 h). |
| **Conflict when switching machines** | Rebase fails on startup | Autosync is **paused** for that repo + notification; I resolve by hand. Nothing is ever lost. |

---

## 11. Edge cases and safety

- **Repo with no commits / no remote:** validate on startup and warn; don't break.
- **Multiple repos:** each with its own independent watcher/timers.
- **Code privacy in the cloud:** by default, hybrid mode prioritizes Ollama (local); if it falls back to the cloud, `cloud_send_content: false` sends only statistics. The API key lives in an environment variable.
- **My manual git operations** while the daemon runs (rebase, branch checkout, etc.): the tool must detect a changed `HEAD`/`rebase in progress`/busy index and **yield** (skip that cycle) instead of fighting. It detects `.git/MERGE_HEAD`, `.git/rebase-*`, the index lock. While it yields, edits are NOT being snapshotted — invisible from the editor — so if the manual operation outlives `BUSY_WARN_SEC` (10 min) it warns ONCE (log + toast) that snapshots are postponed, and notes when they resume. The threshold is high enough that a normal merge — or the transient `index.lock` of any git command — never trips it.
- **Branch guard / branch following.** By default, when HEAD isn't on the configured `branch`, the repo **yields** (no snapshot/seal/autosnap/push on the wrong branch) — `_ensure_on_branch`, rate-limited. With **`track_current_branch: true`** it instead **follows** the current branch: every branch-scoped op uses `st.active_branch` (the live HEAD branch) rather than `cfg.branch`, so snapshot/autosnap/handoff/push all happen on whatever branch you're on (each branch gets its own `refs/autosnap/<user>/<host>/<branch>`, and handoff only matches the same branch). Detached HEAD still yields. Pairs naturally with purist mode (no auto-seal → nothing auto-pushed to the wrong place). Opt-in; default keeps the safe guard.
- **The push targets HEAD** — safe by construction in the shadow model: the branch only
  ever holds sealed and user commits (snapshots live on the side ref). A user's manual
  commit is just… a commit; it rides the next push like a seal would.
- **Restores never destroy unsnapshotted work.** Before `restore_file`/`restore_repo`
  overwrite anything, pending edits are captured into a shadow snapshot — work saved
  since the last snapshot exists nowhere else. Restores write to the WORKTREE only
  (`git restore --worktree` / plain deletes): the user's index stays theirs and the
  restore shows in their `git status` as ordinary edits, captured by a closing snapshot.
  Content the capture pass *can't* take (the filter refused it — excluded / over the
  size limit / binary — or it's untracked and the target tree holds a different version,
  `untracked_collisions`) makes the restore **refuse** where it would TOUCH that
  content, naming the files to copy somewhere safe first: the same policy as the
  handoff apply, because that content exists nowhere in git. Restores also honor the
  branch guard and the busy check, like every other manual operation — off-branch the
  capture would snapshot the wrong branch's chain, and mid-merge/rebase they would
  stomp a conflicted tree.
- **Single instance (no two daemons racing git).** Authoritative guard is a Windows
  named mutex (`acquire_instance_mutex`; no stale-lock problem — the OS releases it on
  process death — and it can't be stolen by an app squatting on the lock port). The
  localhost port (29677, deliberately below Windows' ephemeral range) doubles as a tiny
  command channel: `show` (bring the panel to front), `ping` (presence probe for the
  one-shot guard) and `flushquit` (flush every repo — snapshot + autosnap push — then exit
  cleanly; `build.ps1` uses it to rebuild the very exe that is running without losing work,
  falling back to a forced kill for older daemons, and relaunches after). If a foreign app
  holds the port, single-instance is still enforced by the mutex (we just lose that IPC
  channel). The guard applies to the
  tray **and** `--headless` (a second daemon would race git on the same repos): a second
  tray launch just activates the running panel and exits 0; a second `--headless` refuses
  to start, exit code 2. A headless daemon still answers the activation handshake, so a
  later tray launch detects it and backs off. CLI one-shots check the same guard
  (side-effect-free presence ping) and refuse to run alongside a live daemon — same race —
  unless `--force` is passed.
- **Watcher load.** The watchdog handler drops events for `.git` internals **and** for
  paths matching the repo's excludes (`FileFilter.is_excluded`, a cheap pathspec check, no
  disk I/O) — so a burst like `npm install` under `node_modules/` never wakes the engine.
  Complements *Smart Ignore* (which suggests adding such folders to `extra_excludes`).
- **No orphaned git processes on timeout.** `_run` uses `Popen` + `communicate(timeout=)`,
  and on a timeout kills the **whole process tree** (`taskkill /F /T` on Windows), not just
  `git.exe` — otherwise its children (`ssh.exe`, `git-remote-https.exe`) would linger as
  orphans holding the connection/locks. stdin is always a closed pipe, so a hung credential
  prompt gets EOF (with `GIT_TERMINAL_PROMPT=0`) instead of blocking.
- **No secrets in logs.** The cloud API key is sent in the `x-goog-api-key` header, never in
  the URL (a urllib error often stringifies the URL); the AI failure log also redacts the
  key value defensively.
- **Graceful without `watchdog`.** If the watcher library is missing, the daemon keeps
  running (GUI, manual snapshot/commit, sync, time machine) with a clear warning instead of
  crashing — only automatic change-detection is off.
- **The engine never dies silently.** A failure to even *launch* git (e.g. the repo folder
  vanished: unplugged drive, moved cloud folder) surfaces as `GitError`, so the per-repo
  handling skips that repo — at startup and on every cycle — and the others keep running.
  If the loop still hits an unexpected error, it is made visible instead of leaving a
  zombie tray icon: logged with traceback, emitted as an ERROR event (tray balloon +
  toast), `status()` reports not-running (gray icon), and `--headless` exits with code 1.
- **Power-cut self-healing of refs.** A crash can leave `.git/HEAD` or
  `refs/heads/<branch>` zeroed (NTFS keeps the file size, loses the last small write) —
  git then reports "your current branch appears to be broken" and the repo would yield
  forever. At setup, `GitRepo.repair_corrupt_refs` detects a ref that does not resolve and
  restores it from the newest entry of **its own reflog** whose commit still exists
  (the reflog is append-only and survives the crash) — the manual recovery, automated.
  Conservative: only touches unresolvable refs, never guesses across branches, emits a
  WARNING "repair" event. (Born from a real incident: a power cut zeroed this very
  repo's `refs/heads/main`.)
- **Never `--force`** in the automatic flow.
- **Git output language** forced to English (`LC_ALL=C`) for consistent logs (our parsing uses locale-independent porcelain/plumbing commands).
- **Maintenance:** `git gc --auto` after each seal **and at least once a day**
  (`Engine.GC_INTERVAL_SEC`, on a background worker), to pack the orphan objects left by
  the amends. The daily trigger is **decoupled from sealing on purpose**: in purist mode
  (`seal_interval_min: inf`) the seal never fires, so without it a long-lived WIP would
  accumulate loose objects unbounded. The same daily worker also **prunes this machine's
  own stale autosnap refs** on the remote — refs whose branch no longer exists locally
  and whose mirror is ≥ 7 days old. Single-writer refs, so the delete is race-free; other
  machines' recovery states are never touched, and the age guard keeps a freshly
  re-cloned repo (disaster recovery) from pruning states it hasn't recreated yet.
- **Disabling intervals/limits:** any interval (`*_interval_*`, `debounce_sec`) or size
  threshold (`max_file_bytes`, `max_include_bytes`) accepts a *disable sentinel*
  (`inf`/`off`/`none`/`never`, also bare `None`/`False`), normalized to `math.inf` in
  `RepoConfig.__post_init__`. `inf` flows through the deadline arithmetic untouched (a
  due-time of `inf` is never reached; `min(x, inf) == x`), so the engine needs **no**
  special-casing. The headline use is **purist mode** (`seal_interval_min: inf`): no
  automatic seal — the permanent history is built only by manual **Smart Commit**, while
  the WIP + `autosnap` keep providing the safety net. (YAML only parses `.inf` as a
  float; a bare `inf`/`off` arrives as a string/bool, which is why the normalization
  exists.)
- **Purist commit nudge (`suggest_commit`, default on).** Purist mode's one footgun is a
  branch that silently stagnates when the user forgets to Smart Commit (the work is safe
  in the WIP/autosnap, just not ON the branch — easy to mistake for "pushed"). The engine
  nudges (notification + log) when ALL hold: purist mode, un-sealed work exists, the repo
  has been **quiet** ~20 min (the "you finished something" proxy — state-based, not a
  clock alarm), the last permanent commit is >1 day old, and no nudge fired within a day.
  Sealing resets the staleness gate, so committing silences it by itself. Constants:
  `Engine.COMMIT_NUDGE_*`; no-op when auto-seal is on.

---

## 12. Roadmap by phases

**✅ Phase 1 — MVP (automatic local historian) — COMPLETE:**
- Config + repo validation.
- Watcher + debounce + snapshot (shadow side ref) every 5 min (+ initial snapshot on startup).
- Text/size filter.
- Sealing every 6 h with a **fallback message**.
- Logging.
> With this I already have a versioned time machine, which is 80% of the value.

**✅ Phase 2 — AI + remote sync — COMPLETE:**
- Hybrid AI message generator (Ollama → Gemini → fallback). Never blocks the seal.
- Push of sealed commits (refspec with SHA → `refs/heads/<branch>`) + retry on every sync.
- `fetch` + pull with WIP rebase, only if the remote is ahead; initial sync on startup.
- Conflict policy: abort rebase + pause repo + notify. *(Since superseded: both
  conflict shapes are now covered by the automated suite over throwaway bare remotes
  — see the technical-pending section below and `tests/test_multi_machine.py`.)*

**✅ Phase 4 — Tray UI (PyQt5) — COMPLETE:**
- System tray icon (a "G" with an hourglass, drawn vectorially) whose **color reflects
  the state** (active/paused/conflict/stopped).
- Menu: open panel, pause/resume, sync now, seal now, quit.
- Control panel with tabs Status / Log (filterable by repo, action, level, text) /
  Settings (a form over the defaults) / Advanced (the raw YAML editor).
- Structured event log (`events.jsonl`) + desktop notifications.
- Architecture: engine in a background thread, GUI on the main thread, communication via
  Qt signals; manual actions serialized with a lock in the engine.
- Startup: `python -m sincrogit --tray` (or `pythonw` without a console).

**✅ File history / restore ("time machine") — COMPLETE:**
- Browse a file's past versions, merging the reachable history (sealed commits,
  permanent) and the reflog (intra-window snapshots, ~30 days), collapsing
  identical contents.
- Preview any version and restore it (`git checkout <sha> -- file`); the restore
  becomes a new snapshot, so it is itself versioned.
- CLI: `--history FILE` (interactive) / `--history FILE --pick N` (non-interactive).
- GUI: control panel → the Time machine tab (pin a file for its history).

**Phase 3 — Deployment (partial):**
- ✅ Standalone single-file `SincroGit.exe` (GUI + CLI) via PyInstaller
  (`--onefile --noconsole`); CLI output attaches to the launching terminal.
- ✅ Single-instance lock (localhost socket, no stale-lock problem): a second
  launch asks the running one to show its panel and exits. *(Since superseded: the
  authoritative guard is now a Windows named mutex, shared by tray, headless and the
  CLI one-shots; the port remains as the activation/ping channel. See §11.)*
- ✅ Config resolution: next to the .exe → `%APPDATA%\SincroGit\` → cwd; a default
  is created on first run.
- ✅ Start at log-on: per-user Run key via the Settings checkbox or `--autostart on|off`,
  with doctor reporting and stale-entry self-heal (§9 has the decision).
- ✅ `status` / `log` CLI views (read-only, daemon-safe): per-repo glance and the
  panel's event stream in the terminal, with repo/action/level filters.
- ✅ Cross-machine settings inheritance: a repo publishes its per-repo options to a
  single-writer side ref `refs/sincro/config/<user>` on every autosnap (no-op when
  unchanged); adding the repo on another of your machines OFFERS to inherit them (a
  one-time copy at add, not a live sync). Same identity model as autosnap/handoff.
- ✅ Distribution decision: **portable single exe** instead of an installer — the folder
  the exe sits in IS the installation (config found/created there, with EVERY option in
  the generated template, enforced by an introspective test). The only machine-global
  trace is the optional start-at-login Run entry, which self-heals on relocation.
- ✅ `sincrogit doctor` health check (git, config, each repo's branch/remote,
  read reachability + push credentials, pandoc, AI backends, daemon) — `--doctor`,
  with its own test suite (`tests/test_doctor.py`).
- ⏳ Pending: guided "Add repo" onboarding (create/connect a private remote and verify it
  with a test push, from the GUI) — for the non-Git audience the remote/credentials
  setup is the real entry barrier, not the daemon.

**Pending — technical (no user-visible feature):**
- ⏳ Automated test suite — **exists** (`tests/`, pytest, 150+ tests): restore refusals
  and rename-safe restore, selective restore, timeline, export, history search, config
  surgery, `--doctor`, busy warning, state precedence, diff rendering, offscreen GUI
  dialogs — plus the **multi-machine paths over throwaway bare remotes**:
  `work_relationship` classification (all four verdicts), handoff fast-forward
  (auto/ask/re-validation), the uncaptured-content refusal, handoff across a rename,
  both rebase-conflict shapes, the rejected-push reconcile loop, seal/push idempotence,
  autosnap-ref pruning. Still pending: CI on every push.

**Optional / future:**
- `autosnap` branch with real commits every 5 min (browsable intra-window history *on the remote*) instead of the force-push mirror of the latest state.
- "Live mirror" variant (force-with-lease of the WIP) if real-time remote backup becomes necessary.
- AI batch, aicommit2-inspired (contracts kept: never block the seal, privacy by
  default, stdlib-`urllib` only): a generic **OpenAI-compatible endpoint**
  (`ai.cloud_provider: compatible` + `ai.cloud_url`) covering
  OpenRouter/DeepSeek/LM Studio/Anthropic/… with a single client (keys stay in env
  vars); **messages in the user's language — already done as `ai.language`** (`en`|`es`;
  `ai.language: es` writes seal & Smart Commit messages in Spanish; only generalizing to
  arbitrary locales is pending); **per-repo `ai:` overrides** (e.g. a sensitive repo
  pinned to `mode: local`). See the README → TODO.
- lazygit-inspired batch (lazygit is the complement, not a donor — no git client gets
  rebuilt in the panel): **partial Smart Commit** (file checkbox list — commit the
  selection, return the rest to the recreated WIP; optional `commit_prefix` from the
  branch name), and a docs-only **coexistence recipe** (lazygit `customCommands`
  driving `--commit`/`--apply-handoff`, plus the "don't reword the WIP" warning for
  git clients). See the README → TODO. *(Since partially landed: the coexistence note —
  WIP rules, GitButler — is Manual §9; the `customCommands` snippets remain pending.)*

---

## 13. Decisions made

- ✅ Model **WIP+amend → seal every 6h** + **autosnap** (live mirror) every 30 min.
  *(Since superseded by the v0.2 SHADOW model, below — same rhythms, snapshots moved
  off the user's tip.)*
- ✅ **v0.2: shadow snapshots** (`refs/sincro/wip/<branch>` + a private index) instead
  of a WIP commit at HEAD. Motivation: the WIP at the tip confused every git tool and
  hijacked `git status`/staging. Consequences accepted: a local
  `core.logAllRefUpdates=always` per repo (side refs get no reflog by default — it IS
  the recovery window), and the pull now autostashes the dirty worktree (a conflicting
  stash pop pauses the repo with markers; the pre-pull snapshot guarantees recovery).
  Validated up front with three spikes: textconv gating works tree-vs-tree, the private
  index costs ~120 ms warm on 2 000 files, and a conflicting autostash pop leaves the
  repo *not* mid-rebase (so it's detected via unmerged entries, not is_busy).
- ✅ **Intervals: snapshot every 5 min, seal every 6 h, autosnap every 30 min.**
- ✅ Push **only of sealed commits** (snapshots stay on the side ref; pull always clean; no force-push).
- ✅ **Hybrid** AI (Ollama local → cloud fallback; option to send only stats).
- ✅ **Prefixes:** automatic seal `sincro:`; **manual commit (Smart Commit)** with an AI-proposed Conventional Commits message (cumulative summary since the last manual commit) + timer reset.
- ✅ **Cloud provider: Gemini** (`gemini-2.5-flash-lite`), API key in an environment variable.
- ✅ Filter: **text < 1 MB only**; binaries/large files by hand.
- ✅ **Python**; git via subprocess; `watchdog`; **PyQt5** for the tray UI.
- ✅ Background: **start at log-on**. *(The original decision was a scheduled task;
  building it changed that to the per-user Run key — reasons in §9.)*
- ✅ Working branch: **`main`** (confirm per repo).
- ✅ **Seal every 6 h** (coarse permanent timeline); a manual seal (*Seal now* / `--seal-once` / Smart Commit) for handoff via the clean path.
- ✅ **Periodic pull every 10 min** (`fetch` + pull only if the remote has new commits), besides the startup pull.
- ✅ **Autosnap** (live mirror of the **shadow tip** — the latest snapshot, incl. the live WIP — to `refs/autosnap/<user>/<host>/<branch>`, force-pushed every 30 min, only if it changed): disk-failure RPO ≈ 30 min, cross-machine recovery per file or whole repo (CLI `--autosnaps` + GUI). The "fine browsable history on the remote" variant (one commit per snapshot) is still deferred.

## 14. How to configure the Gemini API key

1. Get a free API key from **Google AI Studio** (`aistudio.google.com`) → *Get API key*.
2. Save it as a **user environment variable** on Windows (not in the YAML, not in the repo):
   ```powershell
   setx SINCROGIT_GEMINI_KEY "your_api_key_here"
   ```
   (Close and reopen the terminal/session for the variable to be available.)
3. The tool reads it from `os.environ["SINCROGIT_GEMINI_KEY"]`.
4. Remember: in hybrid mode it tries **local Ollama first**; Gemini only kicks in if Ollama isn't there. And with `cloud_send_content: false` Gemini only receives file names + `--stat`, not the content.

## 15. Open questions

*(None pending — design closed.)*
